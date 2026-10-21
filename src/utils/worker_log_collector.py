"""
Worker ログ自動収集・集約マネージャー (WorkerLogCollector)
- 各分散ワーカー (OMEN2022, OMEN2017, MasterDebian) の agent.py (ポート 50030) から
  Agentログおよび各TTSエンジンコンテナログをHTTP経由で定期自動収集
- Master側の /app/logs/workers/<node_id>/ 配下にファイルとして保存・更新
- WebUI API および AI・管理者による即時調査（view_file / grep / tail）を完全サポート
- 最大ログ行数制限（自動トリミング）によるディスク肥大化防止
"""

import os
import time
import asyncio
import logging
from typing import Dict, Any, List, Optional
import re
from pathlib import Path
import aiohttp

from src.utils.security import validate_safe_worker_ip, validate_safe_path, is_safe_identifier

logger = logging.getLogger(__name__)

# ホスト/コンテナ内のワーカーログ保存ルートディレクトリ
LOG_DIR_BASE = os.getenv("WORKER_LOG_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs", "workers")))
MAX_LOG_LINES_PER_FILE = 3000   # 1ファイルあたりの最大保持行数 (bot.log と完全同一の3,000行)
KEEP_LINES_ON_ROTATE = 100      # ローテーション時に最新ログに残す直近行数 (100行)


class WorkerLogCollector:
    """各ワーカーノードのログを定期収集してファイル保存するバックグラウンドサービス"""

    def __init__(self, check_interval: float = 60.0):
        self.check_interval = check_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        # 登録ワーカーの既知IPリスト (node_id -> ip)
        self._known_workers: Dict[str, str] = {
            "worker_MASTERSOMEN2022": "192.168.11.200",
            "worker_MASTEROMEN2017": "192.168.11.207",
            "worker_MasterDebian": "192.168.11.248",
            "worker_MasterUbuntu": "192.168.11.245",
        }
        os.makedirs(LOG_DIR_BASE, exist_ok=True)

    def register_worker(self, node_id: str, ip: str) -> None:
        """Worker登録時にIPを動的学習"""
        if node_id and ip and ip not in ("127.0.0.1", "localhost"):
            self._known_workers[node_id] = ip

    async def start(self) -> None:
        """収集ループを開始"""
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4.0))
        self._task = asyncio.create_task(self._collect_loop(), name="worker_log_collector")
        logger.info(f"📁 [WorkerLogCollector] ワーカーログ自動集約サービスを開始しました (保存先: {LOG_DIR_BASE}, 間隔: {self.check_interval:.0f}s)")

    async def stop(self) -> None:
        """収集ループを安全に停止"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("📁 [WorkerLogCollector] ワーカーログ集約サービスを停止しました。")

    async def _collect_loop(self) -> None:
        """定期収集メインループ"""
        # 初回起動時は少し待ってから開始 (各サービス起動待ち)
        await asyncio.sleep(5.0)
        while self._running:
            try:
                await self.collect_all_logs()
            except Exception as e:
                logger.warning(f"WorkerLogCollector periodic collect failed: {e}")
            await asyncio.sleep(self.check_interval)

    async def collect_all_logs(self, tail: int = 300) -> Dict[str, bool]:
        """全既知ワーカーノードからログを並行収集してファイル出力"""
        if not self._known_workers:
            return {}

        results = {}
        tasks = []
        for node_id, ip in list(self._known_workers.items()):
            tasks.append(self.collect_single_worker(node_id, ip, tail=tail))

        collected = await asyncio.gather(*tasks, return_exceptions=True)
        for (node_id, ip), res in zip(self._known_workers.items(), collected):
            if isinstance(res, Exception):
                logger.debug(f"Log collect failed for {node_id} ({ip}): {res}")
                results[node_id] = False
            else:
                results[node_id] = bool(res)
        return results

    async def collect_single_worker(self, node_id: str, ip: str, tail: int = 300) -> bool:
        """単一ワーカーノードから全ログ（Agent＋各エンジン）を取得しファイルへ書き出し"""
        # SSRF ガード (CWE-918): 不正・予約済みIPアドレスへのリクエストを遮断
        if not validate_safe_worker_ip(ip):
            logger.warning(f"SSRF guard blocked request to worker IP: {ip} (node_id={node_id})")
            return False

        url = f"http://{ip}:50030/logs?target=all&tail={tail}"
        session = self._session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4.0))
        own_session = self._session is None

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
        except Exception:
            return False
        finally:
            if own_session and not session.closed:
                await session.close()

        # 出力ディレクトリ安全検証: logs/workers/<safe_node_name>/ (CWE-22)
        safe_node_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', node_id)
        node_dir = validate_safe_path(LOG_DIR_BASE, safe_node_name)
        if not node_dir:
            logger.warning(f"Path injection guard blocked worker directory: {safe_node_name}")
            return False
        node_dir.mkdir(parents=True, exist_ok=True)

        # 1. Agent ログの保存
        agent_logs = data.get("agent_logs") or data.get("logs") or []
        if agent_logs:
            agent_log_path = validate_safe_path(node_dir, "agent.log")
            if agent_log_path:
                self._write_logs_file(str(agent_log_path), agent_logs)

        # 2. 各エンジンコンテナログの保存
        engines = data.get("engines", {})
        for eng_name, eng_info in engines.items():
            safe_eng_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', eng_name)
            eng_logs = eng_info.get("logs", [])
            if eng_logs:
                eng_log_path = validate_safe_path(node_dir, f"{safe_eng_name}.log")
                if eng_log_path:
                    self._write_logs_file(str(eng_log_path), eng_logs)

        # メタデータ (最終同期時刻・稼働ステータス)
        meta_path = validate_safe_path(node_dir, "status.json")
        if meta_path:
            try:
                with open(meta_path, "w", encoding="utf-8") as f:
                    import json
                    json.dump({
                        "node_id": node_id,
                        "ip": ip,
                        "agent_version": data.get("agent_version", "unknown"),
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                        "engines_status": {k: v.get("running", False) for k, v in engines.items()}
                    }, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        return True

    def _write_logs_file(self, filepath: str, lines: List[str]) -> None:
        """ログファイルに書き出し（行数上限時に *-archive.log へ退避し直近100行保持する2段階ハイブリッドローテーション）"""
        target_path = Path(filepath).resolve()
        base_dir = Path(LOG_DIR_BASE).resolve()
        if not target_path.is_relative_to(base_dir):
            logger.warning(f"Path traversal blocked in _write_logs_file: {filepath}")
            return

        try:
            # 既存ファイルがある場合は読み込み
            existing_lines: List[str] = []
            if target_path.exists() and target_path.is_file():
                with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                    existing_lines = [l.rstrip("\r\n") for l in f.readlines()]

            # 既存末尾と新規先頭の重複を排除してマージ
            merged = list(existing_lines)
            existing_set = set(existing_lines[-200:]) if len(existing_lines) >= 200 else set(existing_lines)
            
            _ctrl_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
            for line in lines:
                s_line = _ctrl_re.sub('', line).rstrip("\r\n")
                if s_line and s_line not in existing_set:
                    merged.append(s_line)
                    existing_set.add(s_line)

            # 行数判定: MAX_LOG_LINES_PER_FILE (3000行) を超えた場合はローテーションを実施
            if len(merged) >= MAX_LOG_LINES_PER_FILE:
                older_lines = merged[:-KEEP_LINES_ON_ROTATE]
                recent_lines = merged[-KEEP_LINES_ON_ROTATE:]

                # アーカイブファイルパス: 例 logs/workers/<node_id>/agent-archive.log
                archive_path = target_path.parent / f"{target_path.stem}-archive.log"
                if not archive_path.resolve().is_relative_to(base_dir):
                    return

                # 古い行をアーカイブファイルへ追記退避
                with open(archive_path, "a", encoding="utf-8", errors="replace") as af:
                    af.write("\n".join(older_lines) + "\n")

                # 直近100行をアクティブファイルに上書き
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(recent_lines) + "\n")

                # アーカイブが 100MB を超過した場合は別スレッドで zstd 圧縮を実行 (bot.log と同一)
                if archive_path.exists() and archive_path.stat().st_size >= 100 * 1024 * 1024:
                    try:
                        from src.utils.log_handler import compress_archive_worker
                        import threading
                        t = threading.Thread(
                            target=compress_archive_worker,
                            args=(archive_path,),
                            daemon=True,
                            name=f"zstd-worker-{target_path.stem}"
                        )
                        t.start()
                    except Exception as ce:
                        logger.debug(f"Failed to trigger zstd compression for {archive_path}: {ce}")
            else:
                # 3000行未満の場合はそのまま上書き保存
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(merged) + "\n")
        except Exception as e:
            logger.debug(f"Failed to write log file {filepath}: {e}")

    def get_log_content(self, node_id: str, target: str = "agent", tail: int = 100) -> Dict[str, Any]:
        """保存されているログファイルを読み取って返却 (WebUI / AI用)"""
        safe_node_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', node_id)
        safe_target_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', target.lower())
        
        # 安全なパス検証 (CWE-22)
        log_file_path = validate_safe_path(LOG_DIR_BASE, f"{safe_node_name}/{safe_target_name}.log")
        if not log_file_path or not log_file_path.exists() or not log_file_path.is_file():
            return {
                "success": False,
                "node_id": node_id,
                "target": target,
                "lines": [],
                "error": f"Log file not found: {safe_target_name}.log"
            }

        try:
            _ctrl_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
            with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = [_ctrl_re.sub('', l).rstrip("\r\n") for l in f.readlines()]
            recent = [l for l in (all_lines[-tail:] if tail > 0 else all_lines) if l.strip()]
            return {
                "success": True,
                "node_id": node_id,
                "target": target,
                "file_path": str(log_file_path),
                "total_lines": len(all_lines),
                "lines": recent,
                "mtime": log_file_path.stat().st_mtime
            }
        except Exception as e:
            return {
                "success": False,
                "node_id": node_id,
                "target": target,
                "lines": [],
                "error": str(e)
            }

    def list_available_worker_logs(self) -> List[Dict[str, Any]]:
        """利用可能なワーカーログファイルの一覧を返却"""
        results = []
        if not os.path.exists(LOG_DIR_BASE):
            return results

        for item in sorted(os.listdir(LOG_DIR_BASE)):
            item_path = os.path.join(LOG_DIR_BASE, item)
            if os.path.isdir(item_path):
                files = []
                for fname in sorted(os.listdir(item_path)):
                    if fname.endswith(".log"):
                        fpath = os.path.join(item_path, fname)
                        files.append({
                            "name": fname,
                            "target": fname.replace(".log", ""),
                            "size_bytes": os.path.getsize(fpath),
                            "mtime": os.path.getmtime(fpath)
                        })
                results.append({
                    "node_id": item,
                    "dir_path": item_path,
                    "files": files
                })
        return results


# シングルトンインスタンス
worker_log_collector = WorkerLogCollector(check_interval=60.0)
