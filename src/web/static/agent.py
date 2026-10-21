#!/usr/bin/env python3
"""
TTSBot Compute Worker 自律管理エージェント (tts-worker-agent) v2
- Master (ttsbot) への自動登録 & 定期ハートビート
- Docker ソケット経由でのエンジン起動・停止 リモート制御
- アイドル15分で自動コンテナ停止・メモリOS返却 (共用マシンのリソース完全保護)
- rsyncd 内蔵によるモデル自動同期 (パスワード・SSH設定不要のLAN直接同期)
- 起動時ハードウェアスペック自動検出 (GPU/CPU/RAM) → Master に通知し max_concurrent/priority を動的最適化
- 自己アップデート: Master から /update エンドポイント経由で docker compose pull & restart を自律実行
"""

import os
import sys
import time
import socket
import asyncio
import logging
import platform
import subprocess
import shutil
import io
import tarfile
import zipfile
import hashlib
import re
import http.client
import json
import collections
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
import aiohttp
from aiohttp import web

# タイムゾーンをJST (UTC+9) に統一
os.environ["TZ"] = "Asia/Tokyo"
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

JST = timezone(timedelta(hours=9))

def jst_converter(*args):
    """ロギング用JSTコンバータ (環境・OS問わず確実にJST出力)"""
    return datetime.now(JST).timetuple()

logging.Formatter.converter = jst_converter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("worker-agent")

# 直近ログのインメモリリングバッファ (Web APIで取得可能)
_LOG_BUFFER = collections.deque(maxlen=300)

class BufferLogHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:
            pass

_buf_handler = BufferLogHandler()
_buf_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s]: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_buf_handler)

# Multi-Master URL候補リスト (カンマ区切り対応。未指定時は Ubuntu を第1候補、Debian を第2候補として自動フォールバック)
_raw_master_urls = os.getenv("MASTER_URLS", os.getenv("MASTER_URL", "http://192.168.11.245:8088,http://192.168.11.248:8088"))
MASTER_CANDIDATE_URLS: List[str] = [u.strip().rstrip("/") for u in _raw_master_urls.split(",") if u.strip()]
if not MASTER_CANDIDATE_URLS:
    MASTER_CANDIDATE_URLS = ["http://192.168.11.245:8088"]
_current_master_idx: int = 0

def get_master_url() -> str:
    """現在アクティブに接続している Master URL を取得"""
    global _current_master_idx
    return MASTER_CANDIDATE_URLS[_current_master_idx % len(MASTER_CANDIDATE_URLS)]

def switch_to_next_master() -> str:
    """切断時に次の Master 候補へ切り替え"""
    global _current_master_idx
    if len(MASTER_CANDIDATE_URLS) > 1:
        _current_master_idx = (_current_master_idx + 1) % len(MASTER_CANDIDATE_URLS)
        new_url = get_master_url()
        logger.info(f"🔄 [Multi-Master] 切断検知: 別の Master URL へ切り替えます ➔ {new_url}")
        return new_url
    return get_master_url()

def is_using_primary_master() -> bool:
    """第1優先 (Primary) Master を使用中かどうか"""
    global _current_master_idx
    return _current_master_idx == 0

def try_switch_to_primary_master() -> str:
    """第1優先 Master への自動復帰 (Preempt 追従)"""
    global _current_master_idx
    if _current_master_idx != 0:
        _current_master_idx = 0
        logger.info(f"✨ [Multi-Master] 第1優先 Master (Primary) へ復帰を試みます ➔ {get_master_url()}")
    return get_master_url()

AGENT_PORT = int(os.getenv("AGENT_PORT", "50030"))
RSYNC_PORT = int(os.getenv("RSYNC_PORT", "8730"))
IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "900"))  # 15分
NODE_ID = os.getenv("NODE_ID", f"worker_{socket.gethostname()}")
WORKER_TOKEN = os.getenv("WORKER_SHARED_SECRET", os.getenv("WORKER_SECRET", "")).strip()

def resolve_work_dir() -> str:
    """ホストの作業ディレクトリ（Composeファイルやdockerディレクトリが存在する場所）を特定"""
    if os.path.isdir("/work"):
        return "/work"
    env_dir = os.getenv("WORK_DIR")
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    if os.path.exists("/app/docker-compose.worker.yml"):
        return "/app"
    default_dir = os.path.expanduser("~/ttsbot-worker")
    if os.path.isdir(default_dir):
        return default_dir
    return os.path.abspath(".")

WORK_DIR = resolve_work_dir()
WORKER_COMPOSE_FILE = os.getenv("WORKER_COMPOSE_FILE", os.path.join(WORK_DIR, "docker-compose.worker.yml"))
AGENT_COMPOSE_FILE = os.getenv("AGENT_COMPOSE_FILE", os.path.join(WORK_DIR, "docker-compose.agent.yml"))
# Agent 自身のファイルパス (自己アップデート時に上書き)
AGENT_SCRIPT_PATH = os.getenv("AGENT_SCRIPT_PATH", os.path.abspath(__file__))
# 自動バージョンチェック間隔 (分)
AUTO_UPDATE_CHECK_INTERVAL = int(os.getenv("AUTO_UPDATE_CHECK_INTERVAL", "60"))
# エージェントバージョン (定数 - 定義変更で自動アップデート判定に使用)
AGENT_VERSION = "3.13.2"
# ハートビート経由の自動更新: 同一ノードでの再試行間隔 (秒) と、Master ドレイン後の推論排出待ち (秒)
AUTO_UPDATE_COOLDOWN_SECONDS = 900
AUTO_UPDATE_DRAIN_GRACE_SECONDS = 15
# この Agent プロセスの起動時刻 (Master が再起動完了を判定するために /health で返す)
AGENT_STARTED_AT = time.time()


def version_is_newer(remote: str, local: str) -> bool:
    """"3.12.0" 形式のバージョン比較 (remote > local)"""
    try:
        return [int(x) for x in str(remote).split(".")] > [int(x) for x in str(local).split(".")]
    except Exception:
        return False


def read_local_manifest_version() -> str:
    """適用済みマニフェストバージョン (WORK_DIR/.manifest_version) を取得"""
    try:
        with open(os.path.join(resolve_work_dir(), ".manifest_version"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def normalize_coeiroink_compose_volumes(compose_text: str, hybrid_src: str, ssd_src: str, ram_src: str) -> str:
    r"""
    docker-compose.worker.yml 内の COEIROINK ボリュームマウント行を安全に正規化。
    行頭 (^\s*-\s*) を厳格に判定し、既存のクォートやWindowsパス(C:/)の有無に関わらず安全に置換。
    パス名に含まれるハイフン等による誤マッチや多重挿入を完全防止する。
    """
    lines = []
    for line in compose_text.splitlines():
        if re.search(r':/opt/coeiroink/[^/\s]+/speaker_info["\']?', line):
            m = re.match(r'^(\s*-\s*)', line)
            if m:
                indent = m.group(1)
                dest_m = re.search(r'(:/opt/coeiroink/[^/\s]+/speaker_info)', line)
                if dest_m:
                    line = f'{indent}"{hybrid_src}{dest_m.group(1)}"'
        elif re.search(r':/opt/coeiroink/[^/\s]+/storage/ssd:ro["\']?', line):
            m = re.match(r'^(\s*-\s*)', line)
            if m:
                indent = m.group(1)
                dest_m = re.search(r'(:/opt/coeiroink/[^/\s]+/storage/ssd:ro)', line)
                if dest_m:
                    line = f'{indent}"{ssd_src}{dest_m.group(1)}"'
        elif re.search(r':/opt/coeiroink/[^/\s]+/storage/ram["\']?', line):
            m = re.match(r'^(\s*-\s*)', line)
            if m:
                indent = m.group(1)
                dest_m = re.search(r'(:/opt/coeiroink/[^/\s]+/storage/ram)', line)
                if dest_m:
                    line = f'{indent}"{ram_src}{dest_m.group(1)}"'
        lines.append(line)
    return "\n".join(lines) + ("\n" if compose_text.endswith("\n") else "")



class CoeiroinkWorkerRamManager:
    """
    Workerノード専用 COEIROINK 動的RAM Disk (tmpfs) マネージャー
    - 相対シンボリックリンク二層構造 (hybrid / ram / ssd)
    - ホストメモリ二段階セーフティネット (警戒: 85% / 緊急: 90% / 復帰: 70%)
    - アトミック昇格 (.tmp経由) / 二段階遅延解放
    - 起動時オートヒーリング
    """
    WARNING_THRESHOLD_PERCENT = 85.0
    CRITICAL_THRESHOLD_PERCENT = 90.0
    RECOVERY_THRESHOLD_PERCENT = 70.0
    FREEZE_DURATION_SECONDS = 300.0

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        self.ssd_dir = os.path.join(work_dir, "coeiroink_models")
        self.ram_dir = os.path.join(work_dir, "coeiroink_ram")
        self.hybrid_dir = os.path.join(work_dir, "coeiroink_hybrid")
        self._uuid_to_model_dir: Dict[str, str] = {}
        self._pinned_models: set = set()
        self._active_ram_models: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._freeze_until: float = 0.0

    def setup_hybrid_structure(self):
        os.makedirs(self.ssd_dir, exist_ok=True)
        os.makedirs(self.ram_dir, exist_ok=True)
        os.makedirs(self.hybrid_dir, exist_ok=True)
        self._scan_ssd_models()
        self._auto_heal_symlinks()

    def _scan_ssd_models(self):
        self._uuid_to_model_dir.clear()
        if not os.path.exists(self.ssd_dir):
            return
        for entry in os.scandir(self.ssd_dir):
            if entry.is_dir() and not entry.name.startswith("."):
                metas_path = os.path.join(entry.path, "metas.json")
                if os.path.exists(metas_path):
                    try:
                        with open(metas_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            uuid_val = data.get("speakerUuid")
                            if uuid_val:
                                self._uuid_to_model_dir[uuid_val] = entry.name
                    except Exception:
                        pass
        logger.info(f"🔍 [WorkerRAM] SSDモデルスキャン完了: {len(self._uuid_to_model_dir)} キャラクター検出")

    def _auto_heal_symlinks(self):
        if os.path.exists(self.ram_dir):
            for entry in os.scandir(self.ram_dir):
                if entry.name.endswith(".tmp"):
                    try:
                        shutil.rmtree(entry.path, ignore_errors=True)
                    except Exception:
                        pass
        for model_dir in self._uuid_to_model_dir.values():
            link_path = os.path.join(self.hybrid_dir, model_dir)
            ram_path = os.path.join(self.ram_dir, model_dir)
            if os.path.islink(link_path):
                cur_target = os.readlink(link_path)
                if "../storage/ram/" in cur_target and os.path.exists(ram_path):
                    continue
            tmp_link = os.path.join(self.hybrid_dir, f".{model_dir}.tmp_link")
            try:
                if os.path.exists(tmp_link) or os.path.islink(tmp_link):
                    os.unlink(tmp_link)
                os.symlink(f"../storage/ssd/{model_dir}", tmp_link)
                os.replace(tmp_link, link_path)
            except Exception:
                pass
        logger.info("🛡️ [WorkerRAM] 起動時オートヒーリング完了 (全モデルをSSD参照に安全接続)")

    def resolve_model_dir(self, uuid_or_dir: str) -> Optional[str]:
        if uuid_or_dir in self._uuid_to_model_dir:
            return self._uuid_to_model_dir[uuid_or_dir]
        if uuid_or_dir in self._uuid_to_model_dir.values():
            return uuid_or_dir
        for u, d in self._uuid_to_model_dir.items():
            if d.startswith(uuid_or_dir) or uuid_or_dir.startswith(u):
                return d
        return None

    async def promote_model(self, uuid_or_dir: str) -> bool:
        model_dir = self.resolve_model_dir(uuid_or_dir)
        if not model_dir:
            return False
        now = time.time()
        if self._freeze_until > now:
            return False
        usage = get_resource_usage()
        mem_percent = usage.get("mem_percent") or 0.0
        if mem_percent >= self.WARNING_THRESHOLD_PERCENT:
            logger.warning(f"⚠️ [WorkerRAM] ホストメモリ警戒域 ({mem_percent:.1f}%) のため昇格をブロック: {model_dir}")
            return False

        async with self._lock:
            ram_path = os.path.join(self.ram_dir, model_dir)
            link_path = os.path.join(self.hybrid_dir, model_dir)
            if os.path.exists(ram_path) and os.path.islink(link_path):
                if "../storage/ram/" in os.readlink(link_path):
                    self._active_ram_models[model_dir] = now
                    return True
            ssd_path = os.path.join(self.ssd_dir, model_dir)
            if not os.path.exists(ssd_path):
                return False

            def _copy():
                tmp_dir = os.path.join(self.ram_dir, f"{model_dir}.tmp")
                if os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                ret = subprocess.run(["cp", "-r", ssd_path, tmp_dir], check=False)
                if ret.returncode != 0:
                    raise RuntimeError("cp -r failed")
                if os.path.exists(ram_path):
                    shutil.rmtree(ram_path, ignore_errors=True)
                os.replace(tmp_dir, ram_path)

                tmp_link = os.path.join(self.hybrid_dir, f".{model_dir}.tmp_link")
                if os.path.exists(tmp_link) or os.path.islink(tmp_link):
                    os.unlink(tmp_link)
                os.symlink(f"../storage/ram/{model_dir}", tmp_link)
                os.replace(tmp_link, link_path)

            try:
                t0 = time.time()
                await asyncio.to_thread(_copy)
                self._active_ram_models[model_dir] = time.time()
                logger.info(f"✨ [WorkerRAM] モデル昇格完了: {model_dir} ({time.time() - t0:.2f}s)")
                return True
            except Exception as e:
                logger.error(f"❌ [WorkerRAM] モデル昇格失敗 ({model_dir}): {e}")
                self._switch_link_to_ssd_sync(model_dir)
                return False

    def _switch_link_to_ssd_sync(self, model_dir: str):
        link_path = os.path.join(self.hybrid_dir, model_dir)
        tmp_link = os.path.join(self.hybrid_dir, f".{model_dir}.tmp_link")
        try:
            if os.path.exists(tmp_link) or os.path.islink(tmp_link):
                os.unlink(tmp_link)
            os.symlink(f"../storage/ssd/{model_dir}", tmp_link)
            os.replace(tmp_link, link_path)
        except Exception:
            pass

    async def demote_model(self, model_dir: str, force: bool = False, delay_seconds: float = 3.0) -> bool:
        if not force and model_dir in self._pinned_models:
            return False
        async with self._lock:
            ram_path = os.path.join(self.ram_dir, model_dir)
            if not os.path.exists(ram_path):
                self._active_ram_models.pop(model_dir, None)
                self._switch_link_to_ssd_sync(model_dir)
                return True
            self._switch_link_to_ssd_sync(model_dir)
            self._active_ram_models.pop(model_dir, None)

        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        def _cleanup():
            if os.path.exists(ram_path):
                shutil.rmtree(ram_path, ignore_errors=True)
        await asyncio.to_thread(_cleanup)
        logger.info(f"✨ [WorkerRAM] モデル解放完了: {model_dir}")
        return True

    async def emergency_flush(self):
        usage = get_resource_usage()
        mem_percent = usage.get("mem_percent") or 0.0
        logger.critical(f"🚨 [WorkerRAM] 【自律緊急メモリ全解放】ホストメモリ使用率 {mem_percent:.1f}% >= {self.CRITICAL_THRESHOLD_PERCENT}%！")
        async with self._lock:
            for m in self._uuid_to_model_dir.values():
                self._switch_link_to_ssd_sync(m)
            self._active_ram_models.clear()
            self._freeze_until = time.time() + self.FREEZE_DURATION_SECONDS

        await asyncio.sleep(2.0)
        def _flush():
            if os.path.exists(self.ram_dir):
                for entry in os.scandir(self.ram_dir):
                    try:
                        if entry.is_dir():
                            shutil.rmtree(entry.path, ignore_errors=True)
                        else:
                            os.unlink(entry.path)
                    except Exception:
                        pass
        await asyncio.to_thread(_flush)
        logger.info("🛡️ [WorkerRAM] 自律緊急全解放完了。RAMDiskメモリを全返還しました。")

    async def check_memory_safety(self):
        usage = get_resource_usage()
        mem_percent = usage.get("mem_percent")
        if mem_percent is None:
            return
        if mem_percent >= self.CRITICAL_THRESHOLD_PERCENT:
            await self.emergency_flush()
            return
        if mem_percent >= self.WARNING_THRESHOLD_PERCENT:
            candidates = [
                (m, t) for m, t in self._active_ram_models.items()
                if m not in self._pinned_models
            ]
            if candidates:
                candidates.sort(key=lambda x: x[1])
                await self.demote_model(candidates[0][0], force=False)
        now = time.time()
        if self._freeze_until > 0 and now >= self._freeze_until:
            if mem_percent <= self.RECOVERY_THRESHOLD_PERCENT:
                logger.info(f"✨ [WorkerRAM] メモリ安全圏復帰 ({mem_percent:.1f}%)。昇格凍結を解除しました。")
                self._freeze_until = 0.0

    def pin_model(self, uuid_or_dir: str) -> bool:
        """指定モデルをピン留め（LRU解放・自動スリープ解放の対象外に設定）"""
        model_dir = self.resolve_model_dir(uuid_or_dir)
        if model_dir:
            self._pinned_models.add(model_dir)
            logger.info(f"📌 [WorkerRAM] モデルを常駐ピン留め登録: {model_dir}")
            return True
        return False

    def unpin_model(self, uuid_or_dir: str):
        """ピン留め解除"""
        model_dir = self.resolve_model_dir(uuid_or_dir)
        if model_dir in self._pinned_models:
            self._pinned_models.remove(model_dir)
            logger.info(f"📌 [WorkerRAM] モデルのピン留めを解除: {model_dir}")

    def get_active_model_identifiers(self) -> List[str]:
        """RAM常駐中のモデルディレクトリ名および対応UUIDのリストを返す"""
        res = list(self._active_ram_models.keys())
        dir_to_uuid = {d.lower(): u.lower() for u, d in self._uuid_to_model_dir.items()}
        for m in list(self._active_ram_models.keys()):
            u = dir_to_uuid.get(m.lower())
            if u and u not in res:
                res.append(u)
        return res

    def get_status(self) -> Dict[str, Any]:
        """RAMDisk状態サマリーの取得"""
        usage = get_resource_usage()
        now = time.time()
        return {
            "active_models": list(self._active_ram_models.keys()),
            "active_identifiers": self.get_active_model_identifiers(),
            "pinned_models": list(self._pinned_models),
            "is_frozen": now < self._freeze_until,
            "freeze_remaining_sec": max(0.0, self._freeze_until - now),
            "host_mem_percent": usage.get("mem_percent"),
            "total_models": len(self._uuid_to_model_dir),
        }


ENGINES = {
    "voicevox": {
        "container_name": "ttsworker-voicevox",
        "port": 50021,
        "health_path": "/version",
    },
    "sharevox": {
        "container_name": "ttsworker-sharevox",
        "port": 50025,
        "health_path": "/version",
    },
    "coeiroink": {
        "container_name": "ttsworker-coeiroink",
        "port": 50031,
        "health_path": "/docs",
    }
}


def get_resource_usage() -> Dict[str, Any]:
    """
    リアルタイムのリソース使用状況を取得 (CPU%/メモリ/GPU)
    """
    usage: Dict[str, Any] = {}

    # CPU 使用率 (/proc/stat 1秒差分)
    try:
        def _read_cpu_times():
            with open("/proc/stat") as f:
                line = f.readline()
            parts = line.split()
            total = sum(int(x) for x in parts[1:])
            idle  = int(parts[4])
            return total, idle
        t1, i1 = _read_cpu_times()
        time.sleep(0.2)
        t2, i2 = _read_cpu_times()
        dt, di = t2 - t1, i2 - i1
        usage["cpu_percent"] = round((1 - di / dt) * 100, 1) if dt > 0 else 0.0
    except Exception:
        # /proc/stat がない環境 (Windowsコンテナ内等)はスキップ
        usage["cpu_percent"] = None

    # メモリ (/proc/meminfo)
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.split()[0])  # kB
        total_kb    = mem.get("MemTotal", 0)
        available_kb = mem.get("MemAvailable", 0)
        used_kb     = total_kb - available_kb
        usage["mem_total_gb"]   = round(total_kb    / 1024 / 1024, 1)
        usage["mem_used_gb"]    = round(used_kb     / 1024 / 1024, 1)
        usage["mem_percent"]    = round(used_kb / total_kb * 100, 1) if total_kb else 0.0
    except Exception:
        usage["mem_total_gb"] = None
        usage["mem_used_gb"]  = None
        usage["mem_percent"]  = None

    # GPU (nvidia-smi)
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            usage["gpu_util_percent"]  = float(parts[0].strip()) if len(parts) > 0 else None
            usage["gpu_mem_used_mb"]   = int(parts[1].strip())   if len(parts) > 1 else None
            usage["gpu_mem_total_mb"]  = int(parts[2].strip())   if len(parts) > 2 else None
            usage["gpu_temp_celsius"]  = float(parts[3].strip()) if len(parts) > 3 else None
    except Exception:
        pass

    return usage


def get_local_ip() -> str:
    """LAN内の自機IPアドレスを取得"""
    env_ip = os.getenv("WORKER_IP")
    if env_ip and env_ip.strip():
        return env_ip.strip()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_effective_hostname() -> str:
    """コンテナ外のホスト名またはマシン名を優先取得 (コンテナID 12文字Hexの露出を防止)"""
    h = os.getenv("HOST_HOSTNAME") or os.getenv("HOSTNAME")
    if h and not re.match(r"^[0-9a-fA-F]{12}$", h.strip()):
        return h.strip()
    node_id = os.getenv("NODE_ID", "")
    if node_id.startswith("worker_"):
        nid_sub = node_id[7:].strip()
        if nid_sub and not re.match(r"^[0-9a-fA-F]{12}$", nid_sub):
            return nid_sub
    return socket.gethostname()


def detect_hardware_specs() -> Dict[str, Any]:
    """
    ハードウェアスペックを自動検出して返す。
    Windows: nvidia-smi / wmic を利用
    Linux:   nvidia-smi / /proc/meminfo / nproc を利用
    """
    specs: Dict[str, Any] = {
        "hostname": get_effective_hostname(),
        "platform": platform.system(),
        "cpu_count": os.cpu_count() or 1,
        "total_ram_gb": 0.0,
        "gpu": False,
        "gpu_name": "",
        "gpu_vram_mb": 0,
        "recommended_max_concurrent": 1,
        "recommended_priority": 2,
    }

    # --- RAM ---
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        specs["total_ram_gb"] = round(kb / 1024 / 1024, 1)
                        break
        elif platform.system() == "Windows":
            result = subprocess.run(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if "TotalPhysicalMemory=" in line:
                    specs["total_ram_gb"] = round(int(line.split("=")[1]) / 1024 / 1024 / 1024, 1)
    except Exception as e:
        logger.debug(f"RAM 検出失敗: {e}")

    # --- GPU 検出 (環境変数または nvidia-smi) ---
    env_has_gpu = os.getenv("HAS_GPU", "").lower() in ("true", "1", "yes")
    env_gpu_name = os.getenv("GPU_NAME", "").strip()
    try:
        env_gpu_vram = int(os.getenv("GPU_VRAM_MB", "0") or "0")
    except ValueError:
        env_gpu_vram = 0

    if env_has_gpu:
        specs["gpu"] = True
        specs["gpu_name"] = env_gpu_name
        specs["gpu_vram_mb"] = env_gpu_vram

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            if len(parts) >= 2:
                specs["gpu"] = True
                specs["gpu_name"] = parts[0].strip()
                specs["gpu_vram_mb"] = int(parts[1].strip())
    except Exception as e:
        logger.debug(f"GPU 直接検出スキップ (nvidia-smi): {e}")

    # --- 動的推奨パラメーター計算 ---
    ram = specs["total_ram_gb"]
    cpus = specs["cpu_count"]
    has_gpu = specs["gpu"]
    vram = specs["gpu_vram_mb"]

    # 同時実行数: GPUあり&VRAMが多ければ並列化、CPUのみは保守的に
    if has_gpu and vram >= 8000:
        recommended_concurrent = min(3, max(1, int(vram / 4000)))
        recommended_priority = 1  # GPU ノードは最優先
    elif has_gpu and vram >= 4000:
        recommended_concurrent = 2
        recommended_priority = 1
    elif ram >= 16:
        recommended_concurrent = min(2, max(1, int(ram / 8)))
        recommended_priority = 2
    else:
        recommended_concurrent = 1
        recommended_priority = 2

    # CPU コア数でも上限を設ける
    recommended_concurrent = min(recommended_concurrent, max(1, cpus // 2))

    specs["recommended_max_concurrent"] = recommended_concurrent
    specs["recommended_priority"] = recommended_priority

    # --- 動的リソース割り当て計算 (CPUコア・RAM・スレッド数) ---
    # 1. CPU コア / スレッド数
    if has_gpu:
        # GPUマシン: 推論はCUDA実行のためCPUは前処理/後処理用。
        # 余裕を持たせつつOSや他アプリを阻害しないよう最大8コアまで動的割り当て
        alloc_cpus = max(2, min(8, cpus // 2))
        thread_num = max(2, min(8, alloc_cpus))
    else:
        # CPUマシン (共用サーバー等): 最大半分、かつ共用配慮で最大4コア
        alloc_cpus = max(1, min(4, cpus // 2))
        thread_num = max(1, alloc_cpus)

    # 2. RAM 割り当て
    # 物理RAMの最大 50% を TTS コンテナ全体の使用枠とする
    if ram > 0:
        if has_gpu:
            # GPU専用機: 実RAMの50%を配分 (下限4GB, 上限24GB)
            alloc_ram_gb = max(4.0, min(24.0, ram * 0.5))
        else:
            # CPU共用機: 実RAMの50%だが共用配慮で最大8GB上限
            alloc_ram_gb = max(2.0, min(8.0, ram * 0.5))
    else:
        alloc_ram_gb = 4.0

    # 各エンジンへの配分 (VOICEVOX: 35%, SHAREVOX: 30%, COEIROINK: 35%)
    ram_vv = max(1, int(round(alloc_ram_gb * 0.35)))
    ram_sv = max(1, int(round(alloc_ram_gb * 0.30)))
    ram_ci = max(2 if not has_gpu else 4, int(round(alloc_ram_gb * 0.35)))

    specs["alloc_cpus"] = alloc_cpus
    specs["thread_num"] = thread_num
    specs["alloc_ram_gb"] = alloc_ram_gb
    specs["ram_vv"] = ram_vv
    specs["ram_sv"] = ram_sv
    specs["ram_ci"] = ram_ci

    logger.info(
        f"🖥️  ハードウェア検出: RAM={ram}GB (割当計={alloc_ram_gb}GB: VV={ram_vv}G/SV={ram_sv}G/CI={ram_ci}G) "
        f"CPU={cpus}コア (割当={alloc_cpus}コア/スレッド={thread_num}) "
        f"GPU={'あり (' + specs['gpu_name'] + ' ' + str(vram) + 'MB)' if has_gpu else 'なし'} "
        f"→ 推奨並列数={recommended_concurrent} 優先度={recommended_priority}"
    )
    return specs


class DockerAPI:
    """Docker UNIX Socket (/var/run/docker.sock) 経由で Docker Engine API を直接操作する軽量クライアント"""
    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        self.socket_path = socket_path

    def is_available(self) -> bool:
        return os.path.exists(self.socket_path)

    def request(self, method: str, path: str, body: Optional[Any] = None, timeout: int = 30) -> Tuple[int, Any]:
        if not self.is_available():
            return -1, "docker.sock not found"
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
            conn = http.client.HTTPConnection("localhost")
            conn.sock = sock
            headers = {"Host": "localhost"}
            payload = None
            if body is not None:
                payload = json.dumps(body)
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", errors="replace")
            conn.close()
            try:
                parsed = json.loads(data)
            except Exception:
                parsed = data
            return resp.status, parsed
        except Exception as e:
            return -1, str(e)

    def inspect_container(self, name_or_id: str) -> Optional[Dict[str, Any]]:
        status, data = self.request("GET", f"/containers/{name_or_id}/json")
        if status == 200 and isinstance(data, dict):
            return data
        return None

    def is_running(self, name: str) -> bool:
        data = self.inspect_container(name)
        if data:
            return bool(data.get("State", {}).get("Running", False))
        return False

    def start_container(self, name: str) -> Tuple[int, Any]:
        return self.request("POST", f"/containers/{name}/start")

    def stop_container(self, name: str, timeout: int = 5) -> Tuple[int, Any]:
        return self.request("POST", f"/containers/{name}/stop?t={timeout}")

    def restart_container(self, name: str, timeout: int = 5) -> Tuple[int, Any]:
        return self.request("POST", f"/containers/{name}/restart?t={timeout}")

    def remove_container(self, name: str, force: bool = True) -> Tuple[int, Any]:
        """コンテナを強制停止・削除 (Conflict防止)"""
        try:
            self.stop_container(name, timeout=2)
        except Exception:
            pass
        return self.request("DELETE", f"/containers/{name}?force={'true' if force else 'false'}&v=true")

    def ensure_image(self, image_tag: str) -> bool:
        status, _ = self.request("GET", f"/images/{image_tag}/json")
        if status == 200:
            return True
        img, tag = image_tag.split(":", 1) if ":" in image_tag else (image_tag, "latest")
        logger.info(f"📥 Docker イメージ {image_tag} を pull しています...")
        status, _ = self.request("POST", f"/images/create?fromImage={img}&tag={tag}", timeout=180)
        return status in (200, 204)

    def exec_cmd(self, name: str, cmd: List[str], timeout: int = 15) -> Tuple[int, str]:
        """コンテナ内でコマンドを実行して出力を取得"""
        payload = {
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
            "Cmd": cmd
        }
        status, res = self.request("POST", f"/containers/{name}/exec", body=payload, timeout=timeout)
        if status != 201 or not isinstance(res, dict) or "Id" not in res:
            return -1, f"Failed to create exec: {res}"
        exec_id = res["Id"]
        status, out = self.request("POST", f"/exec/{exec_id}/start", body={"Detach": False, "Tty": False}, timeout=timeout)
        if isinstance(out, str):
            clean_lines = []
            for line in out.splitlines():
                if len(line) >= 8 and line[:8].isascii():
                    clean_lines.append(line[8:])
                else:
                    clean_lines.append(line)
            return status, "\n".join(clean_lines)
        return status, str(out)

    def get_container_logs(self, name: str, tail: int = 50) -> str:
        status, out = self.request("GET", f"/containers/{name}/logs?stdout=true&stderr=true&tail={tail}", timeout=5)
        if isinstance(out, str):
            clean_lines = []
            _ctrl_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
            for line in out.splitlines():
                # 先頭8バイトにDocker Multiplexヘッダー (0x01/0x02等) がある場合は切り落とす
                if len(line) >= 8 and (ord(line[0]) in (1, 2) or ord(line[0]) == 0):
                    line = line[8:]
                # NULLバイトおよびバイナリ制御文字を完全除去
                sanitized = _ctrl_re.sub('', line).strip('\r')
                if sanitized.strip():
                    clean_lines.append(sanitized)
            return "\n".join(clean_lines)
        return str(out)

    def get_host_work_dir(self, agent_container_name: str = "ttsworker-agent") -> Optional[str]:
        """Agent コンテナの Labels, Mounts または Binds からホスト側の作業ディレクトリのパスを特定"""
        data = self.inspect_container(agent_container_name)
        if not data:
            return None

        # 0) Compose v2 の標準ラベル com.docker.compose.project.working_dir を最優先 (最も確実)
        labels = data.get("Config", {}).get("Labels") or {}
        comp_wd = labels.get("com.docker.compose.project.working_dir")
        if comp_wd:
            is_posix = comp_wd.startswith("/")
            is_win = len(comp_wd) >= 2 and comp_wd[0].isalpha() and comp_wd[1] == ":"
            if is_posix or is_win:
                return comp_wd

        # 1) トップレベルの Mounts を検索 (Docker Engine API 標準)
        mounts = data.get("Mounts") or []
        for m in mounts:
            if isinstance(m, dict):
                dest = m.get("Destination", "")
                src = m.get("Source", "")
                if dest == "/work" and src:
                    return src
        for m in mounts:
            if isinstance(m, dict):
                dest = m.get("Destination", "")
                src = m.get("Source", "")
                if dest == "/app" and src:
                    parent = src.rstrip("/\\")
                    for _ in range(2):
                        p_base = os.path.basename(parent).lower()
                        if p_base in ("agent", "docker"):
                            parent = os.path.dirname(parent)
                    return parent

        # 2) HostConfig.Binds を検索 (レガシー)
        binds = data.get("HostConfig", {}).get("Binds") or []
        for b in binds:
            if ":/work" in b:
                return b.split(":/work")[0]
        for b in binds:
            if ":/app" in b:
                host_app = b.split(":/app")[0]
                parent = host_app.rstrip("/\\")
                for _ in range(2):
                    p_base = os.path.basename(parent).lower()
                    if p_base in ("agent", "docker"):
                        parent = os.path.dirname(parent)
                return parent
        return None

    def get_compose_project_name(self, compose_file_name: str = "") -> str:
        """Compose ファイルに応じて完全分離されたプロジェクト名を返す"""
        fname = compose_file_name.lower()
        if "agent" in fname:
            return "ttsbot-worker-agent"
        if "worker" in fname or "engine" in fname:
            return "ttsbot-worker-engines"

        # 不明な場合のみ既存エンジンコンテナまたはホストディレクトリ名から取得 (ttsworker-agent は除外)
        for cname in ["ttsworker-voicevox", "ttsworker-coeiroink", "ttsworker-sharevox"]:
            data = self.inspect_container(cname)
            if data:
                proj = data.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
                if proj and proj != "ttsbot-worker-agent":
                    return proj
        host_work = self.get_host_work_dir() or ""
        base = os.path.basename(host_work.rstrip("/\\"))
        return base or "ttsbot-worker-engines"

    def run_compose_via_helper(
        self,
        work_dir: str,
        compose_file_name: str,
        compose_args: List[str],
        detach: bool = False,
        timeout: int = 300
    ) -> Tuple[int, str]:
        """docker:cli ヘルパーコンテナを起動して docker compose コマンドを実行"""
        if not self.is_available():
            return 1, "docker.sock not available"

        host_work = self.get_host_work_dir() or work_dir
        proj_name = self.get_compose_project_name(compose_file_name)
        self.ensure_image("docker:cli")

        # 既存コンテナとのConflictを防ぐため、up時はプロジェクト名指定と--force-recreateを付与
        args = list(compose_args)
        if "up" in args and "--force-recreate" not in args:
            args.append("--force-recreate")

        # エンジンコンテナ起動時は、過去に別プロジェクトやゾンビとして作られた同名コンテナをシェルレベルで強制排除
        cleanup_prefix = ""
        if "worker" in compose_file_name.lower() and "up" in args:
            service_cnames = {
                "worker-voicevox": "ttsworker-voicevox",
                "worker-sharevox": "ttsworker-sharevox",
                "worker-coeiroink": "ttsworker-coeiroink",
            }
            specified = [service_cnames[s] for s in service_cnames if s in args]
            if specified:
                cleanup_prefix = f"docker rm -f {' '.join(specified)} 2>/dev/null || true; "
            else:
                cleanup_prefix = "docker rm -f ttsworker-voicevox ttsworker-sharevox ttsworker-coeiroink 2>/dev/null || true; "

        cmd_str = f"{cleanup_prefix}docker compose -p {proj_name} -f {compose_file_name} " + " ".join(args)
        if detach:
            cmd_str = f"sleep 2 && {cmd_str}"

        # Windows パス (C:\Users\...) を Docker Binds 用に正規化 (//c/Users/...)
        bind_work = host_work
        if len(bind_work) >= 2 and bind_work[0].isalpha() and bind_work[1] == ":":
            drive = bind_work[0].lower()
            rest = bind_work[2:].replace("\\", "/").lstrip("/")
            bind_work = f"//{drive}/{rest}"

        payload = {
            "Image": "docker:cli",
            "Cmd": ["sh", "-c", cmd_str],
            "WorkingDir": "/work",
            "HostConfig": {
                "Binds": [
                    "/var/run/docker.sock:/var/run/docker.sock",
                    f"{bind_work}:/work"
                ],
                "AutoRemove": not detach
            }
        }

        helper_name = f"tts-compose-helper-{int(time.time())}"
        status, res = self.request("POST", f"/containers/create?name={helper_name}", body=payload)
        if status not in (200, 201) or not isinstance(res, dict) or "Id" not in res:
            return 1, f"Failed to create helper container: {res}"

        cid = res["Id"]
        status, _ = self.request("POST", f"/containers/{cid}/start")
        if status not in (200, 204):
            return 1, f"Failed to start helper container: status={status}"

        if detach:
            return 0, "Helper container started in background"

        status, wait_res = self.request("POST", f"/containers/{cid}/wait", timeout=timeout)
        exit_code = wait_res.get("StatusCode", 1) if isinstance(wait_res, dict) else 1

        status, raw_logs = self.request("GET", f"/containers/{cid}/logs?stdout=1&stderr=1", timeout=30)
        log_text = raw_logs if isinstance(raw_logs, str) else str(raw_logs)

        # 終了後に明示的破棄
        self.request("DELETE", f"/containers/{cid}?force=1")
        return exit_code, log_text

docker_api = DockerAPI()


def ensure_compose_project_isolation(work_dir: str) -> bool:
    """
    docker-compose.worker.yml と docker-compose.agent.yml のプロジェクト名を完全物理分離。
    同一プロジェクト判定によるコンテナ誤削除（--remove-orphans や Compose 操作時の自己削除）を恒久防止。
    """
    updated = False
    # 1. worker.yml (engines)
    w_path = os.path.join(work_dir, "docker-compose.worker.yml")
    if os.path.exists(w_path):
        try:
            with open(w_path, "r", encoding="utf-8") as f:
                w_content = f.read()
            if "name: ttsbot-worker-engines" not in w_content:
                if re.search(r"^name:\s*.*$", w_content, re.MULTILINE):
                    w_new = re.sub(r"^name:\s*.*$", "name: ttsbot-worker-engines", w_content, flags=re.MULTILINE)
                else:
                    w_new = "name: ttsbot-worker-engines\n\n" + w_content
                with open(w_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(w_new)
                logger.info(f"🛡️ {w_path} に 'name: ttsbot-worker-engines' を自動設定しました")
                updated = True
        except Exception as e:
            logger.warning(f"⚠️ {w_path} のプロジェクト名分離設定失敗: {e}")

    # 2. agent.yml (agent)
    a_path = os.path.join(work_dir, "docker-compose.agent.yml")
    if os.path.exists(a_path):
        try:
            with open(a_path, "r", encoding="utf-8") as f:
                a_content = f.read()
            changed = False
            a_new = re.sub(r'image:\s*["\']?python:[0-9.]+(-slim)?["\']?', 'image: python:3.14-slim', a_content)
            if a_new != a_content:
                changed = True
            if "name: ttsbot-worker-agent" not in a_new:
                if re.search(r"^name:\s*.*$", a_new, re.MULTILINE):
                    a_new = re.sub(r"^name:\s*.*$", "name: ttsbot-worker-agent", a_new, flags=re.MULTILINE)
                else:
                    a_new = "name: ttsbot-worker-agent\n\n" + a_new
                changed = True
            if changed:
                with open(a_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(a_new)
                logger.info(f"🛡️ {a_path} に 'name: ttsbot-worker-agent' を自動設定しました")
                updated = True
        except Exception as e:
            logger.warning(f"⚠️ {a_path} のプロジェクト名分離設定失敗: {e}")

    return updated

def ensure_agent_compose_python314(compose_path: str) -> bool:
    """互換性維持ラッパー"""
    work_dir = os.path.dirname(compose_path) or resolve_work_dir()
    return ensure_compose_project_isolation(work_dir)


def run_docker_cmd(*args) -> Tuple[int, str]:
    """Docker コマンドを実行 (CLI があれば CLI、なければ Docker Socket API 経由)"""
    if shutil.which("docker"):
        cmd = ["docker", *args]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
            return res.returncode, res.stdout.strip() or res.stderr.strip()
        except Exception as e:
            return 1, str(e)

    # CLI がない場合の API フォールバック
    if not docker_api.is_available():
        return 1, "docker CLI and docker.sock are not available"

    if len(args) >= 1:
        subcmd = args[0]
        if subcmd == "inspect" and len(args) >= 4 and args[1] == "-f" and args[2] == "{{.State.Running}}":
            cname = args[3]
            running = docker_api.is_running(cname)
            return (0, "true") if running else (0, "false")
        elif subcmd == "start" and len(args) >= 2:
            cname = args[1]
            status, res = docker_api.start_container(cname)
            return (0, "started") if status in (200, 204, 304) else (1, str(res))
        elif subcmd == "stop" and len(args) >= 2:
            cname = args[-1]
            status, res = docker_api.stop_container(cname, 5)
            return (0, "stopped") if status in (200, 204, 304) else (1, str(res))
        elif subcmd == "restart" and len(args) >= 2:
            cname = args[-1]
            status, res = docker_api.restart_container(cname, 5)
            return (0, "restarted") if status in (200, 204) else (1, str(res))

    return 1, f"Unsupported docker command without CLI: {args}"


def is_container_running(container_name: str) -> bool:
    """コンテナが起動中かチェック"""
    if docker_api.is_available():
        return docker_api.is_running(container_name)
    code, out = run_docker_cmd("inspect", "-f", "{{.State.Running}}", container_name)
    return code == 0 and out.lower() == "true"


def is_container_existing(container_name: str) -> bool:
    """コンテナがDocker上に実在するか (停止中含む) をチェック"""
    if docker_api.is_available():
        return docker_api.inspect_container(container_name) is not None
    code, out = run_docker_cmd("inspect", "-f", "{{.State.Status}}", container_name)
    if code != 0 or "no such" in out.lower() or "not found" in out.lower():
        return False
    return True


def get_container_status(container_name: str) -> str:
    """コンテナのState.Status (running, restarting, exited, not_found等) を取得"""
    if docker_api.is_available():
        data = docker_api.inspect_container(container_name)
        if not data:
            return "not_found"
        return str(data.get("State", {}).get("Status", "unknown")).lower()
    code, out = run_docker_cmd("inspect", "-f", "{{.State.Status}}", container_name)
    if code != 0 or "no such" in out.lower() or "not found" in out.lower():
        return "not_found"
    return out.strip().lower()


class WorkerAgent:
    def __init__(self):
        self.node_id = NODE_ID
        self.local_ip = get_local_ip()
        self.registered_to_master: bool = False
        self.specs = detect_hardware_specs()
        self.last_used_time: Dict[str, float] = {k: 0.0 for k in ENGINES}
        self.rsync_proc: Optional[subprocess.Popen] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._update_lock = asyncio.Lock()
        self._last_resource_usage: Dict[str, Any] = {}
        self._last_resource_time: float = 0.0
        self._starting_engines: set = set()  # 起動処理中のエンジン（停止割り込み保護用）
        self.engine_healthy: Dict[str, bool] = {k: True for k in ENGINES}  # 直近ヘルスプローブ結果
        self.engine_fail_count: Dict[str, int] = {k: 0 for k in ENGINES}   # 連続失敗カウンタ (3回で再起動)
        self.engine_restart_count: Dict[str, int] = {k: 0 for k in ENGINES} # 連続再起動カウンタ (2回でLevel-2クリーン再作成)

        # Compose プロジェクト完全物理分離の自己点検・補完
        try:
            ensure_compose_project_isolation(resolve_work_dir())
        except Exception as e:
            logger.debug(f"Compose プロジェクト名自己点検スキップ: {e}")

        # COEIROINK 動的RAMDisk マネージャー初期化
        try:
            self.ram_manager = CoeiroinkWorkerRamManager(resolve_work_dir())
            self.ram_manager.setup_hybrid_structure()
        except Exception as e:
            logger.error(f"⚠️ [WorkerRAM] CoeiroinkWorkerRamManager初期化エラー: {e}")
            self.ram_manager = None


    async def ensure_engine_stack_exists(self) -> bool:
        """全エンジンコンテナの実在性を検証し、欠損していればComposeから自動復元 (スタック消失の恒久防止)"""
        missing = []
        for eng, meta in ENGINES.items():
            cname = meta["container_name"]
            if not is_container_existing(cname):
                missing.append(cname)

        if not missing:
            return True

        logger.warning(f"⚠️ 欠落しているエンジンコンテナを検出しました: {missing}。docker compose up で自動復元します...")
        work_dir = resolve_work_dir()
        compose_fname = os.path.basename(WORKER_COMPOSE_FILE)
        code = 1
        if shutil.which("docker"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose", "-f", WORKER_COMPOSE_FILE, "up", "-d",
                    cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                _, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
                code = proc.returncode
            except Exception as e:
                logger.error(f"❌ スタック自動復元失敗: {e}")
                code = 1
        elif docker_api.is_available():
            code, out_up = await asyncio.to_thread(
                docker_api.run_compose_via_helper,
                work_dir,
                compose_fname,
                ["up", "-d"],
                False,
                180
            )

        if code == 0:
            logger.info(f"✅ エンジンスタック ({missing}) を docker compose up で正常に自動復元しました。")
            return True
        else:
            logger.error(f"❌ エンジンスタック自動復元失敗: code={code}")
            return False

    async def recreate_engine_container(self, engine_name: str) -> bool:
        """破損・クラッシュループしたコンテナを docker rm -f で強制破棄し、Compose からクリーン再生成 (Level-2 Auto-Heal)"""
        if engine_name not in ENGINES:
            return False
        meta = ENGINES[engine_name]
        cname = meta["container_name"]
        svc_name = f"worker-{engine_name}"
        work_dir = resolve_work_dir()
        compose_fname = os.path.basename(WORKER_COMPOSE_FILE)

        logger.critical(f"🧹 [Clean Recreate] '{engine_name}' ({cname}) を強制削除してクリーン再作成を開始します...")
        if docker_api.is_available():
            docker_api.remove_container(cname, force=True)
        else:
            run_docker_cmd("rm", "-f", cname)

        code = 1
        if shutil.which("docker"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose", "-f", WORKER_COMPOSE_FILE, "up", "-d", svc_name,
                    cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                _, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                code = proc.returncode
            except Exception as e:
                logger.error(f"❌ コンテナクリーン再作成失敗 ({svc_name}): {e}")
                code = 1
        elif docker_api.is_available():
            code, out_up = await asyncio.to_thread(
                docker_api.run_compose_via_helper,
                work_dir,
                compose_fname,
                ["up", "-d", svc_name],
                False,
                120
            )

        if code == 0:
            logger.info(f"✅ [Clean Recreate] '{engine_name}' ({cname}) のクリーン再作成に成功しました！")
            self.engine_restart_count[engine_name] = 0
            self.engine_fail_count[engine_name] = 0
            return True
        else:
            logger.error(f"❌ [Clean Recreate] '{engine_name}' ({cname}) のクリーン再作成に失敗: code={code}")
            return False

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0))
        return self._session

    def start_rsync_daemon(self):
        """内蔵 rsync サーバーを起動し、モデル同期を直接受け入れ (rsync がインストールされている環境のみ)"""
        if not shutil.which("rsync"):
            logger.info("ℹ️ rsync コマンドは未検出のためデーモンはスキップします (HTTP 自動同期を使用)。")
            return

        conf_content = f"""
pid file = /tmp/rsyncd.pid
log file = /tmp/rsyncd.log
use chroot = no
port = {RSYNC_PORT}
address = 0.0.0.0

[models]
path = /coeiroink_models
comment = COEIROINK Models Volume
read only = no
list = yes
hosts allow = *
"""
        try:
            with open("/tmp/rsyncd.conf", "w") as f:
                f.write(conf_content)

            cmd = ["rsync", "--daemon", "--config=/tmp/rsyncd.conf", "--no-detach"]
            self.rsync_proc = subprocess.Popen(cmd)
            logger.info(f"✅ 内蔵 rsync サーバーがポート {RSYNC_PORT} で稼働開始しました。")
        except Exception as e:
            logger.warning(f"⚠️ rsync デーモンの起動に失敗しました: {e}")

    async def start_engine(self, engine_name: str) -> bool:
        """指定エンジンのコンテナを起動し、ヘルスチェック通過を待機"""
        if engine_name not in ENGINES:
            return False

        meta = ENGINES[engine_name]
        cname = meta["container_name"]
        self.last_used_time[engine_name] = time.time()

        if is_container_running(cname):
            return True

        self._starting_engines.add(engine_name)
        try:
            logger.info(f"⚡ [Engine Start] '{engine_name}' ({cname}) を起動中...")
            code, out = run_docker_cmd("start", cname)
            if code != 0:
                if "No such container" in str(out) or "not found" in str(out).lower():
                    logger.warning(f"⚠️ コンテナ '{cname}' が未作成/存在しないため、docker compose up で自動生成を試行します...")
                    svc_name = f"worker-{engine_name}"
                    work_dir = resolve_work_dir()
                    compose_fname = os.path.basename(WORKER_COMPOSE_FILE)
                    if shutil.which("docker"):
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "docker", "compose", "-f", WORKER_COMPOSE_FILE, "up", "-d", svc_name,
                                cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                            )
                            _, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                            code = proc.returncode
                        except Exception as e:
                            logger.error(f"❌ docker compose up 失敗 ({svc_name}): {e}")
                            code = 1
                    elif docker_api.is_available():
                        code, out_up = await asyncio.to_thread(
                            docker_api.run_compose_via_helper,
                            work_dir,
                            compose_fname,
                            ["up", "-d", svc_name],
                            False,
                            120
                        )
                    if code != 0:
                        logger.error(f"❌ コンテナ自動作成・起動失敗 ({cname}): code={code}")
                        return False
                    logger.info(f"✅ コンテナ '{cname}' を docker compose up で正常に自動作成・起動しました。")
                else:
                    logger.error(f"❌ コンテナ起動失敗 ({cname}): {out}")
                    return False

            # ヘルスチェック待機 (最大60秒、ホスト到達可能IPを自動フォールバック)
            session = await self.get_session()
            local_ip = get_local_ip()
            candidate_hosts = ["127.0.0.1"]
            if local_ip and local_ip != "127.0.0.1" and local_ip not in candidate_hosts:
                candidate_hosts.append(local_ip)
            candidate_hosts.append("host.docker.internal")

            for _ in range(120):
                for h in candidate_hosts:
                    health_url = f"http://{h}:{meta['port']}{meta['health_path']}"
                    try:
                        async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=0.8)) as resp:
                            if resp.status in (200, 404):
                                logger.info(f"✅ '{engine_name}' が正常に起動しました (Health OK via {h})。")
                                self.engine_healthy[engine_name] = True
                                self.engine_fail_count[engine_name] = 0
                                self.engine_restart_count[engine_name] = 0
                                return True
                    except Exception:
                        pass
                await asyncio.sleep(0.5)

            logger.warning(f"⚠️ '{engine_name}' のヘルスチェック待機がタイムアウトしました。")
            return True
        finally:
            self._starting_engines.discard(engine_name)

    def stop_engine(self, engine_name: str) -> bool:
        """指定エンジンのコンテナを停止してリソースを解放"""
        if engine_name not in ENGINES:
            return False

        if engine_name in self._starting_engines:
            logger.warning(f"🛡️ [Engine Protect] '{engine_name}' は起動処理中のため停止指示を安全に無視・保護します。")
            return True

        cname = ENGINES[engine_name]["container_name"]
        if not is_container_running(cname):
            return True

        logger.info(f"💤 [Engine Stop] '{engine_name}' ({cname}) を停止してメモリを解放します...")
        code, out = run_docker_cmd("stop", "-t", "5", cname)
        if code == 0:
            self.engine_healthy[engine_name] = True
            self.engine_fail_count[engine_name] = 0
            self.engine_restart_count[engine_name] = 0
        return code == 0

    async def auto_idle_monitor(self):
        """アイドル監視ループ: 15分未使用のエンジンを自動停止 (VC通話中はスリープ抑制) ＆ RAMDisk安全性監視"""
        while True:
            await asyncio.sleep(30)
            now = time.time()

            # ホストメモリの二段階セーフティネット監視 (85% LRU解放 / 90% 緊急全解放 / 70% 復帰)
            if self.ram_manager:
                try:
                    await self.ram_manager.check_memory_safety()
                except Exception as e:
                    logger.debug(f"RAMDisk memory safety check exception: {e}")

            if getattr(self, "any_human_in_vc", False):
                # VCに通話者がいる間は、各エンジンの最終使用時刻を更新して常駐・0ms即答を維持 (課題A-2)
                for eng in ENGINES:
                    self.last_used_time[eng] = now
                continue

            for eng, meta in ENGINES.items():
                cname = meta["container_name"]
                if is_container_running(cname):
                    last = self.last_used_time.get(eng, 0.0)
                    if last > 0 and (now - last) >= IDLE_TIMEOUT_SECONDS:
                        logger.info(f"⏰ [Auto Sleep] '{eng}' はVC完全無人かつ15分間未使用のため自動停止します。")
                        self.stop_engine(eng)


    async def fix_coeiroink_mount(self) -> Dict[str, Any]:
        """
        COEIROINK のバインドマウント元をホスト絶対パスに修正してコンテナを即座に再起動
        """
        work_dir = resolve_work_dir()
        compose_file = WORKER_COMPOSE_FILE
        if not os.path.exists(compose_file):
            return {"success": False, "error": f"Compose file not found: {compose_file}"}

        host_work = docker_api.get_host_work_dir() or work_dir
        if len(host_work) >= 2 and host_work[0].isalpha() and host_work[1] == ":":
            norm_work = host_work.replace("\\", "/").rstrip("/")
        elif host_work.startswith("/"):
            norm_work = host_work.rstrip("/")
        else:
            norm_work = "."

        hybrid_mount_src = f"{norm_work}/coeiroink_hybrid"
        ssd_mount_src = f"{norm_work}/coeiroink_models"
        ram_mount_src = f"{norm_work}/coeiroink_ram"

        # 1. Compose ファイル内のマウント記述を置換
        try:
            with open(compose_file, "r", encoding="utf-8") as f:
                content = f.read()

            new_content = normalize_coeiroink_compose_volumes(content, hybrid_mount_src, ssd_mount_src, ram_mount_src)
            if new_content != content:
                with open(compose_file, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new_content)
                logger.info(f"📝 Compose ファイルのマウント元を正規化しました: hybrid={hybrid_mount_src}, ssd={ssd_mount_src}, ram={ram_mount_src}")
        except Exception as e:
            return {"success": False, "error": f"Failed to rewrite compose: {e}"}

        # 2. 既存の ttsworker-coeiroink を削除
        logger.info("🧹 ttsworker-coeiroink を再作成のため強制削除中...")
        docker_api.remove_container("ttsworker-coeiroink", force=True)
        if shutil.which("docker"):
            try:
                subprocess.run(["docker", "rm", "-f", "ttsworker-coeiroink"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except Exception:
                pass

        # 3. worker-coeiroink サービスを up -d
        has_docker_cli = bool(shutil.which("docker"))
        compose_fname = os.path.basename(compose_file)
        if has_docker_cli:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose", "-f", compose_file, "up", "-d", "--force-recreate", "worker-coeiroink",
                    cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                logger.info(f"✅ docker compose up worker-coeiroink 完了 (rc={proc.returncode})")
            except Exception as e:
                return {"success": False, "error": f"docker compose exec error: {e}"}
        else:
            logger.info("ℹ️ docker:cli ヘルパー経由で worker-coeiroink を起動します")
            try:
                code, out = await asyncio.to_thread(
                    docker_api.run_compose_via_helper,
                    work_dir,
                    compose_fname,
                    ["up", "-d", "--force-recreate", "worker-coeiroink"],
                    False,
                    120
                )
                logger.info(f"✅ ヘルパー経由 docker compose up worker-coeiroink 完了 (rc={code})")
            except Exception as e:
                return {"success": False, "error": f"helper exec error: {e}"}

        # 4. ヘルスチェック待機 & speakers 検証
        session = await self.get_session()
        spk_url = "http://127.0.0.1:50031/v1/speakers"
        speakers = []
        for _ in range(60):
            try:
                async with session.get(spk_url, timeout=aiohttp.ClientTimeout(total=1.0)) as resp:
                    if resp.status == 200:
                        speakers = await resp.json()
                        break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        logger.info(f"🎉 COEIROINK マウント修復完了！ 認識話者数: {len(speakers)} 件")
        return {
            "success": True,
            "hybrid_mount_src": hybrid_mount_src,
            "ssd_mount_src": ssd_mount_src,
            "ram_mount_src": ram_mount_src,
            "speaker_count": len(speakers),
            "speakers": [s.get("speakerName") for s in speakers[:5]]
        }

    async def ensure_coeiroink_mount_healthy(self):
        """
        起動時および定期監視で、COEIROINK のマウントパス整合性と話者数健全性を自律担保する。
        - Compose ファイル内の相対マウントパス (./ や /work/) をホスト絶対パスへ事前自動正規化
        - もしコンテナ稼働中かつ認識話者数が0件（ホスト側SSDモデル数は存在する）場合、自律修復 (fix_coeiroink_mount) を発動
        """
        await asyncio.sleep(5)  # 他サービスの初期化完了を待機
        work_dir = resolve_work_dir()
        compose_file = WORKER_COMPOSE_FILE
        if not os.path.exists(compose_file):
            return

        host_work = docker_api.get_host_work_dir() or work_dir
        if not host_work or host_work == "." or host_work == "/work":
            return

        if len(host_work) >= 2 and host_work[0].isalpha() and host_work[1] == ":":
            norm_work = host_work.replace("\\", "/").rstrip("/")
        elif host_work.startswith("/"):
            norm_work = host_work.rstrip("/")
        else:
            return

        hybrid_mount_src = f"{norm_work}/coeiroink_hybrid"
        ssd_mount_src = f"{norm_work}/coeiroink_models"
        ram_mount_src = f"{norm_work}/coeiroink_ram"

        # 1. Compose ファイル内のマウント記述のパス確認＆自動修正
        try:
            with open(compose_file, "r", encoding="utf-8") as f:
                content = f.read()

            new_content = normalize_coeiroink_compose_volumes(content, hybrid_mount_src, ssd_mount_src, ram_mount_src)
            if new_content != content:
                with open(compose_file, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new_content)
                logger.info(f"🛡️ [WorkerRAM] Compose のマウントパスをホスト絶対パスへ自動正規化しました: {hybrid_mount_src}")
        except Exception as e:
            logger.debug(f"ensure_coeiroink_mount_healthy rewrite check failed: {e}")

        # 2. ttsworker-coeiroink が動いている場合、話者数を検査
        if is_container_running("ttsworker-coeiroink"):
            session = await self.get_session()
            spk_url = "http://127.0.0.1:50031/v1/speakers"
            try:
                async with session.get(spk_url, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                    if resp.status == 200:
                        speakers = await resp.json()
                        ssd_count = len(getattr(self.ram_manager, "_uuid_to_model_dir", {}))
                        if len(speakers) == 0 and ssd_count > 0:
                            logger.warning(f"⚠️ [WorkerRAM] COEIROINK の認識話者数が0件です (SSD上には{ssd_count}件)。自律修復を発動します...")
                            await self.fix_coeiroink_mount()
            except Exception:
                pass

    async def register_to_master(self) -> bool:
        """Master サーバーに自分自身を自動登録（ハードウェアスペックも送信）"""
        session = await self.get_session()
        master_url = get_master_url()
        url = f"{master_url}/api/worker/register"

        mac = self.specs["recommended_max_concurrent"]
        pri = self.specs["recommended_priority"]

        payload = {
            "node_id": self.node_id,
            "node_name": f"Worker ({socket.gethostname()})",
            "ip": self.local_ip,
            "agent_port": AGENT_PORT,
            "rsync_port": RSYNC_PORT,
            "specs": self.specs,
            "engines": {
                k: {
                    "port": v["port"],
                    "priority": pri,
                    "max_concurrent": mac
                }
                for k, v in ENGINES.items()
            }
        }
        headers = {"X-Worker-Token": WORKER_TOKEN} if WORKER_TOKEN else {}
        try:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.registered_to_master = True
                    logger.info(f"🎉 Master への自動登録成功！ (url={master_url}, concurrent={mac}, priority={pri})")
                    return True
                else:
                    self.registered_to_master = False
                    err = await resp.text()
                    logger.warning(f"⚠️ Master 登録エラー ({resp.status}): {err}")
        except Exception as e:
            logger.debug(f"Master ({master_url}) 未接続 (次回再試行): {e}")
        return False

    async def heartbeat_loop(self):
        """Master への定期ハートビート送信ループ (通常25秒間隔 / 切断時5秒高速リトライ ＆ Multi-Master自律切替)"""
        # 初回登録を試みる
        registered = await self.register_to_master()
        retry_count = 0
        interval = 25 if registered else 5
        last_primary_check_time = time.time()

        while True:
            await asyncio.sleep(interval)
            session = await self.get_session()

            # 第2候補に接続中なら、60秒ごとに第1候補(Primary)への復帰を試みる
            if not is_using_primary_master() and time.time() - last_primary_check_time > 60.0:
                last_primary_check_time = time.time()
                primary_url = MASTER_CANDIDATE_URLS[0]
                try:
                    async with session.get(f"{primary_url}/health", timeout=aiohttp.ClientTimeout(total=3.0)) as chk_resp:
                        if chk_resp.status == 200:
                            try_switch_to_primary_master()
                            await self.register_to_master()
                except Exception:
                    pass

            master_url = get_master_url()
            url = f"{master_url}/api/worker/heartbeat"

            # リソース情報を非同期取得 (2秒タイムアウト保護でイベントループの遅延・ブロッキングを完全遮断)
            try:
                resource_info = await asyncio.wait_for(
                    asyncio.to_thread(get_resource_usage),
                    timeout=2.0
                )
                self._last_resource_usage = resource_info
                self._last_resource_time = time.time()
            except Exception as res_err:
                logger.debug(f"Resource collection timeout/error, using cached info: {res_err}")
                resource_info = self._last_resource_usage or {}

            payload = {
                "node_id": self.node_id,
                "ip": self.local_ip,
                "agent_port": AGENT_PORT,
                "agent_version": AGENT_VERSION,
                "specs": self.specs,
                "resources": resource_info,
                "ram_models": self.ram_manager.get_active_model_identifiers() if self.ram_manager else [],
                "manifest_version": read_local_manifest_version(),
                "updating": self._update_lock.locked(),
                "engines": {
                    eng: {
                        "running": is_container_running(meta["container_name"]),
                        "port": meta["port"],
                        "healthy": self.engine_healthy.get(eng, True),
                    }
                    for eng, meta in ENGINES.items()
                }
            }
            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                    if resp.status == 404:
                        # 未登録の場合は再登録を試行
                        ok = await self.register_to_master()
                        interval = 25 if ok else 5
                    elif resp.status == 200:
                        self.registered_to_master = True
                        interval = 25
                        if retry_count > 0:
                            logger.info(f"✨ Master ({master_url}) への再接続・復帰に成功しました (通常25秒間隔へ復帰)。")
                        retry_count = 0
                        try:
                            hb_res = await resp.json()
                            self.any_human_in_vc = hb_res.get("any_human_in_vc", False)
                            # Master 調停型の自動更新 (ハートビート相乗り)
                            if "update_available" in hb_res:
                                self._master_coordinates_updates = True
                                if hb_res.get("update_available"):
                                    self._maybe_start_coordinated_update(hb_res)
                        except Exception:
                            pass
                    else:
                        interval = 5
            except Exception:
                retry_count += 1
                interval = 5
                # 2回連続失敗(約10秒)で次のMaster候補へ自律切替
                if len(MASTER_CANDIDATE_URLS) > 1 and retry_count >= 2 and retry_count % 2 == 0:
                    switch_to_next_master()
                    await self.register_to_master()
                if retry_count % 12 == 0:
                    logger.warning(f"⚠️ Master ({get_master_url()}) に {retry_count * 5}秒間接続できていません (5秒間隔で高速再試行中)。")

    def _maybe_start_coordinated_update(self, hb_res: Dict[str, Any]) -> None:
        """
        Master から更新許可 (update_available=True) を受けた場合に更新を開始する。
        Master は同時に1台だけ許可を出し、許可と同時に当ノードをドレイン(割り当て停止)する。
        """
        if self._update_lock.locked():
            return
        now = time.time()
        last = getattr(self, "_last_auto_update_at", 0.0)
        if now - last < AUTO_UPDATE_COOLDOWN_SECONDS:
            return
        target_agent = str(hb_res.get("target_agent_version") or "")
        target_manifest = str(hb_res.get("target_manifest_version") or "")
        need_agent = bool(target_agent) and version_is_newer(target_agent, AGENT_VERSION)
        need_full = bool(target_manifest) and target_manifest != read_local_manifest_version()
        if not (need_agent or need_full):
            return
        self._last_auto_update_at = now
        logger.info(
            f"🆕 [AutoUpdate] Master から更新許可を受信 (Agent: {AGENT_VERSION} → {target_agent or '-'}, "
            f"Manifest: {read_local_manifest_version() or '-'} → {target_manifest or '-'})。"
            f"{AUTO_UPDATE_DRAIN_GRACE_SECONDS}秒後に更新を開始します。"
        )

        async def _run():
            # Master 側ドレイン後、実行中の推論が捌けるのを待つ
            await asyncio.sleep(AUTO_UPDATE_DRAIN_GRACE_SECONDS)
            if not self._update_lock.locked():
                await self.self_update(update_agent=need_agent, update_engines=True, full=need_full)

        asyncio.create_task(_run())

    async def engine_watchdog_loop(self):
        """
        常駐Watchdog監視ループ (20秒周期):
        - 稼働中エンジンのAPIエンドポイントを直接アクティブプローブして健全性を確認
        - 3回連続無応答（約60秒間ハング・無応答状態）を検知した場合は docker restart で自動自己修復 (Auto-Heal)
        """
        logger.info("🐕 [Watchdog] Worker Engine Watchdog loop started.")
        while True:
            try:
                await asyncio.sleep(20)
                session = await self.get_session()
                local_ip = get_local_ip()
                candidate_hosts = ["127.0.0.1"]
                if local_ip and local_ip != "127.0.0.1" and local_ip not in candidate_hosts:
                    candidate_hosts.append(local_ip)
                candidate_hosts.append("host.docker.internal")

                for eng, meta in ENGINES.items():
                    cname = meta["container_name"]
                    status = get_container_status(cname)

                    # 1) コンテナ未実在の検知 (スタック消失の恒久防止)
                    if status == "not_found":
                        logger.critical(
                            f"🚨 [Watchdog] '{eng}' ({cname}) のコンテナが存在しません（未作成または消失）。"
                            f"docker compose up による自動復元をトリガーします！"
                        )
                        asyncio.create_task(self.start_engine(eng))
                        continue

                    # 2) クラッシュループ (Status=Restarting) の早期検知 & Level-2 クリーン再作成
                    if status == "restarting":
                        cnt = self.engine_fail_count.get(eng, 0) + 1
                        self.engine_fail_count[eng] = cnt
                        logger.warning(f"⚠️ [Watchdog] '{eng}' ({cname}) はクラッシュループ (Status=Restarting) 中です ({cnt}/3回)。")
                        if cnt >= 2:
                            logger.critical(
                                f"🚨 [Watchdog Level-2 Auto-Heal] '{eng}' ({cname}) のクラッシュループ（内部状態破損）を検知しました。"
                                f"コンテナをクリーン再作成します！"
                            )
                            self.engine_fail_count[eng] = 0
                            asyncio.create_task(self.recreate_engine_container(eng))
                        continue

                    # 3) 停止中スリープのコンテナ (ハングではなく正常停止のため健全状態として扱う)
                    if status != "running":
                        if not self.engine_healthy.get(eng, True) or self.engine_fail_count.get(eng, 0) > 0:
                            self.engine_healthy[eng] = True
                            self.engine_fail_count[eng] = 0
                        continue

                    if eng in self._starting_engines:
                        continue

                    # 4) 稼働中コンテナのアクティブプローブ (ヘルスチェック)
                    probe_ok = False
                    for h in candidate_hosts:
                        health_url = f"http://{h}:{meta['port']}{meta['health_path']}"
                        try:
                            async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=1.5)) as resp:
                                if resp.status in (200, 404):
                                    probe_ok = True
                                    break
                        except Exception:
                            pass

                    if probe_ok:
                        if not self.engine_healthy.get(eng, True) or self.engine_fail_count.get(eng, 0) > 0:
                            logger.info(f"✨ [Watchdog] '{eng}' が正常応答に復帰しました (Health OK)。")
                        self.engine_healthy[eng] = True
                        self.engine_fail_count[eng] = 0
                        self.engine_restart_count[eng] = 0
                    else:
                        cnt = self.engine_fail_count.get(eng, 0) + 1
                        self.engine_fail_count[eng] = cnt
                        logger.warning(
                            f"⚠️ [Watchdog] '{eng}' ({cname}) からのヘルスチェック応答がありません "
                            f"(失敗 {cnt}/3 回)。"
                        )
                        if cnt >= 3:
                            self.engine_healthy[eng] = False
                            rst_cnt = self.engine_restart_count.get(eng, 0) + 1
                            self.engine_restart_count[eng] = rst_cnt
                            self.engine_fail_count[eng] = 0

                            if rst_cnt >= 2:
                                # 再起動しても復帰しなかった場合 -> Level-2 クリーン再作成！
                                logger.critical(
                                    f"🚨 [Watchdog Level-2 Auto-Heal] '{eng}' ({cname}) は再起動後も無応答が継続しています (再起動回数={rst_cnt})。"
                                    f"内部破損と判断し、コンテナをクリーン再作成します！"
                                )
                                asyncio.create_task(self.recreate_engine_container(eng))
                            else:
                                # 1回目の無応答 -> Level-1 通常再起動 (docker restart)
                                logger.critical(
                                    f"🚨 [Watchdog Auto-Heal] '{eng}' ({cname}) が3回連続無応答（ハング状態）を検知しました。"
                                    f"コンテナを自律再起動 (docker restart) します！"
                                )
                                code, out = run_docker_cmd("restart", "-t", "5", cname)
                                if code == 0:
                                    logger.info(f"✅ [Watchdog Auto-Heal] '{eng}' の再起動コマンドが完了しました。")
                                else:
                                    logger.error(f"❌ [Watchdog Auto-Heal] '{eng}' の再起動に失敗しました: {out}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Error in engine watchdog loop: {e}")

    @staticmethod
    def _calculate_file_sha256(filepath: str, chunk_size: int = 256 * 1024) -> str:
        """ローカルファイルの SHA-256 ハッシュを計算"""
        hasher = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _verify_model_manifest(target_dir: str, manifest: dict) -> Tuple[bool, List[dict]]:
        """
        ローカルモデルディレクトリの全ファイルがマニフェストと合致するか厳密検証。
        - 存在確認
        - サイズ照合
        - SHA-256 チェックサム照合
        返り値: (is_valid: bool, corrupted_or_missing: List[dict])
        """
        missing_or_corrupt = []
        files = manifest.get("files", [])
        if not files:
            return False, []

        for f_info in files:
            rel_path = f_info.get("path", "")
            expected_size = f_info.get("size", 0)
            expected_sha256 = f_info.get("sha256", "")
            local_path = os.path.join(target_dir, rel_path.replace("/", os.sep))

            if not os.path.isfile(local_path):
                missing_or_corrupt.append(f_info)
                continue

            try:
                if os.path.getsize(local_path) != expected_size:
                    missing_or_corrupt.append(f_info)
                    continue

                actual_sha = WorkerAgent._calculate_file_sha256(local_path)
                if actual_sha != expected_sha256:
                    missing_or_corrupt.append(f_info)
                    continue
            except Exception:
                missing_or_corrupt.append(f_info)

        return (len(missing_or_corrupt) == 0, missing_or_corrupt)

    @staticmethod
    def _atomic_promote_dir(staging_dir: str, target_dir: str) -> bool:
        """
        ステージングディレクトリ（.staging_*）を本番ディレクトリへ安全にアトミック昇格。
        Windows環境のロック競合を防ぐため、既存があれば一時バックアップへ退避してから昇格。
        """
        old_dir = f"{target_dir}.old_{int(time.time()*1000)}"
        try:
            if os.path.exists(target_dir):
                os.rename(target_dir, old_dir)
            os.rename(staging_dir, target_dir)
            if os.path.exists(old_dir):
                shutil.rmtree(old_dir, ignore_errors=True)
            return True
        except Exception as e:
            logger.error(f"❌ アトミック昇格失敗 ({staging_dir} -> {target_dir}): {e}")
            if not os.path.exists(target_dir) and os.path.exists(old_dir):
                try:
                    os.rename(old_dir, target_dir)
                except Exception:
                    pass
            return False

    async def _download_single_model_file(self, folder_name: str, file_info: dict, target_dir: str) -> bool:
        """単一ファイルを差分取得し、SHA-256を検証して安全に配置"""
        rel_path = file_info.get("path", "")
        expected_sha256 = file_info.get("sha256", "")
        expected_size = file_info.get("size", 0)

        local_path = os.path.join(target_dir, rel_path.replace("/", os.sep))
        staging_file = f"{local_path}.dl_tmp"
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        url = f"{get_master_url()}/api/worker/models/file/{folder_name}/{rel_path}"
        session = await self.get_session()

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=None, sock_connect=20.0, sock_read=60.0)) as resp:
                if resp.status != 200:
                    logger.warning(f"⚠️ ファイル取得失敗 ({rel_path}): HTTP {resp.status}")
                    return False
                with open(staging_file, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)

            # サイズとハッシュの検証
            if os.path.getsize(staging_file) != expected_size:
                logger.warning(f"⚠️ ファイルサイズ不一致 ({rel_path}): got {os.path.getsize(staging_file)}, expected {expected_size}")
                if os.path.exists(staging_file):
                    os.remove(staging_file)
                return False

            actual_sha = WorkerAgent._calculate_file_sha256(staging_file)
            if actual_sha != expected_sha256:
                logger.warning(f"⚠️ ファイルSHA-256不一致 ({rel_path})")
                if os.path.exists(staging_file):
                    os.remove(staging_file)
                return False

            if os.path.exists(local_path):
                os.remove(local_path)
            os.rename(staging_file, local_path)
            return True
        except Exception as e:
            logger.warning(f"⚠️ ファイル同期エラー ({rel_path}): {e}")
            if os.path.exists(staging_file):
                try:
                    os.remove(staging_file)
                except Exception:
                    pass
            return False

    async def sync_coeiroink_models(self, work_dir: str):
        """
        Master から COEIROINK モデルを同期（Rsync並みの修復力・アトミック・差分同期）
        1. Master から全モデル一覧 (tree_sha256 含む) を取得
        2. 既存モデルはマニフェスト照合（SHA-256検証）
           - 完全一致なら 0秒スキップ
           - 破損・不足が一部のみなら差分ファイルのみピンポイント取得
        3. 新規または大幅破損モデルは .staging_* に安全ストリーミング展開 ➔ SHA-256検証 ➔ アトミック昇格
        4. 破損残骸は本番ディレクトリに1バイトも残さない
        """
        models_dir = os.path.join(work_dir, "coeiroink_models")
        os.makedirs(models_dir, exist_ok=True)

        # 過去の中断されたステージング一時ディレクトリ（.staging_*）を自動クリーンアップ
        try:
            for item in os.listdir(models_dir):
                if item.startswith(".staging_") or ".old_" in item:
                    p = os.path.join(models_dir, item)
                    shutil.rmtree(p, ignore_errors=True)
                    logger.debug(f"🧹 過去のステージング残骸をクリーンアップ: {item}")
        except Exception:
            pass

        session = await self.get_session()
        list_url = f"{get_master_url()}/api/worker/models/list"
        try:
            async with session.get(list_url, timeout=aiohttp.ClientTimeout(total=20.0)) as resp:
                if resp.status != 200:
                    logger.warning(f"⚠️ モデル一覧取得失敗: HTTP {resp.status}")
                    return
                data = await resp.json()
                models = data.get("models", [])
        except Exception as e:
            logger.warning(f"⚠️ モデル一覧取得エラー: {e}")
            return

        total_models = len(models)
        synced_count = 0
        diff_repaired_count = 0
        full_download_count = 0

        for idx, m in enumerate(models, 1):
            folder_name = m.get("folder_name") or m.get("model_name")
            spk_name = m.get("speaker_name", folder_name)
            size_mb = m.get("size_mb", 0)

            if not folder_name:
                continue

            target_dir = os.path.join(models_dir, folder_name)

            # Master から当該モデルの詳細マニフェスト（ファイル単位のSHA256ハッシュ）を取得
            manifest = None
            try:
                mf_url = f"{get_master_url()}/api/worker/models/manifest/{folder_name}"
                async with session.get(mf_url, timeout=aiohttp.ClientTimeout(total=20.0)) as mf_resp:
                    if mf_resp.status == 200:
                        manifest = await mf_resp.json()
            except Exception as e:
                logger.debug(f"マニフェスト取得失敗 ({folder_name}): {e}")

            # Case A: 既存モデルの検証 & 差分修復
            if os.path.exists(target_dir) and manifest:
                await asyncio.sleep(0.01)  # イベントループに制御を戻す
                is_valid, corrupt_files = await asyncio.to_thread(WorkerAgent._verify_model_manifest, target_dir, manifest)
                if is_valid:
                    logger.info(f"✨ [{idx}/{total_models}] モデル '{spk_name}' ({folder_name}, {size_mb}MB) は完全同期済み（SHA-256検証済）です (スキップ)")
                    synced_count += 1
                    continue

                # 差分修復判定: 不足・破損ファイルが全ファイルの40%以下なら差分同期を実行
                total_files = manifest.get("total_files", len(corrupt_files))
                if 0 < len(corrupt_files) <= max(10, int(total_files * 0.4)):
                    logger.info(f"🔄 [{idx}/{total_models}] モデル '{folder_name}' に差分・破損 {len(corrupt_files)}件を検知。差分修復を実行します...")
                    repair_success = True
                    for cf in corrupt_files:
                        ok = await self._download_single_model_file(folder_name, cf, target_dir)
                        if not ok:
                            repair_success = False
                            break

                    if repair_success:
                        # 修復後の再検証
                        await asyncio.sleep(0.01)
                        is_now_valid, _ = await asyncio.to_thread(WorkerAgent._verify_model_manifest, target_dir, manifest)
                        if is_now_valid:
                            logger.info(f"✅ [{idx}/{total_models}] モデル '{spk_name}' ({folder_name}) の差分修復が完了しました！")
                            diff_repaired_count += 1
                            continue

                logger.warning(f"⚠️ [{idx}/{total_models}] モデル '{folder_name}' の差分修復不可/大幅破損のためクリーン再取得します。")

            # Case B: 新規取得または大幅破損時の「アトミック・ステージング展開」
            staging_dir = os.path.join(models_dir, f".staging_{folder_name}_{int(time.time()*1000)}")
            os.makedirs(staging_dir, exist_ok=True)

            logger.info(f"📥 [{idx}/{total_models}] モデル '{spk_name}' ({folder_name}, {size_mb}MB) をステージング領域へダウンロード・展開中...")
            dl_url = f"{MASTER_URL}/api/worker/models/download/{folder_name}"
            stream_timeout = aiohttp.ClientTimeout(total=None, sock_connect=30.0, sock_read=120.0)

            try:
                async with session.get(dl_url, timeout=stream_timeout) as dl_resp:
                    if dl_resp.status != 200:
                        logger.warning(f"⚠️ モデル {folder_name} ダウンロード失敗: HTTP {dl_resp.status}")
                        shutil.rmtree(staging_dir, ignore_errors=True)
                        continue

                    tar_proc = await asyncio.create_subprocess_exec(
                        "tar", "-xf", "-", "-C", staging_dir,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        async for chunk in dl_resp.content.iter_chunked(256 * 1024):
                            tar_proc.stdin.write(chunk)
                            await tar_proc.stdin.drain()
                        tar_proc.stdin.close()
                        _, stderr = await tar_proc.communicate()
                        if tar_proc.returncode != 0:
                            err_msg = stderr.decode(errors="replace").strip()
                            logger.error(f"❌ モデル {folder_name} tar展開失敗 (rc={tar_proc.returncode}): {err_msg}")
                            shutil.rmtree(staging_dir, ignore_errors=True)
                            continue
                    except Exception as pipe_err:
                        tar_proc.kill()
                        shutil.rmtree(staging_dir, ignore_errors=True)
                        raise pipe_err

                # tar 展開後の配置チェック (staging_dir/{folder_name} または staging_dir 直下)
                extracted_dir = os.path.join(staging_dir, folder_name) if os.path.isdir(os.path.join(staging_dir, folder_name)) else staging_dir

                # マニフェスト照合（SHA-256完全性チェック）
                is_valid = True
                if manifest:
                    await asyncio.sleep(0.01)
                    is_valid, corrupt_files = await asyncio.to_thread(WorkerAgent._verify_model_manifest, extracted_dir, manifest)
                    if not is_valid:
                        logger.error(f"❌ モデル '{folder_name}' の展開後ハッシュ検証に失敗しました ({len(corrupt_files)}件不一致)。安全のため破棄します。")
                        shutil.rmtree(staging_dir, ignore_errors=True)
                        continue

                # アトミック昇格 (ステージングから本番へ安全にリネーム)
                if WorkerAgent._atomic_promote_dir(extracted_dir, target_dir):
                    logger.info(f"✅ モデル '{spk_name}' ({folder_name}) のアトミック展開・検証が完了しました。")
                    full_download_count += 1
                else:
                    logger.error(f"❌ モデル '{folder_name}' のアトミック昇格に失敗しました。")

                if os.path.exists(staging_dir):
                    shutil.rmtree(staging_dir, ignore_errors=True)

            except Exception as e:
                logger.error(f"❌ モデル {folder_name} 同期エラー: {e}")
                if os.path.exists(staging_dir):
                    shutil.rmtree(staging_dir, ignore_errors=True)

        logger.info(
            f"📊 [ModelsSync] COEIROINK モデル同期完了: 全{total_models}件中 "
            f"{synced_count}件検証スキップ, {diff_repaired_count}件差分修復, {full_download_count}件新規アトミック展開"
        )
        if (diff_repaired_count > 0 or full_download_count > 0) and docker_api.is_running("ttsworker-coeiroink"):
            logger.info("🔄 [ModelsSync] モデル更新に伴い、COEIROINK エンジンを再起動してモデルをリロードします...")
            try:
                self.stop_engine("coeiroink")
                await asyncio.sleep(2)
                await self.start_engine("coeiroink")
                logger.info("✅ [ModelsSync] COEIROINK エンジン再起動完了 (モデルリロード済)")
            except Exception as r_err:
                logger.warning(f"⚠️ [ModelsSync] 再起動スキップ/エラー: {r_err}")


    async def reconfigure_stack(self) -> Dict[str, Any]:
        """
        Master の /api/worker/manifest を取得し、Dockerfile / Compose を最新構成で書き出して再ビルド・再起動
        """
        result: Dict[str, Any] = {"success": False, "steps": []}
        session = await self.get_session()
        manifest_url = f"{get_master_url()}/api/worker/manifest"
        try:
            async with session.get(manifest_url, timeout=aiohttp.ClientTimeout(total=15.0)) as resp:
                if resp.status != 200:
                    err = f"Failed to get manifest: HTTP {resp.status}"
                    logger.error(f"❌ {err}")
                    result["error"] = err
                    return result
                manifest = await resp.json()
        except Exception as e:
            err = f"Manifest fetch error: {e}"
            logger.error(f"❌ {err}")
            result["error"] = err
            return result

        work_dir = resolve_work_dir()
        has_gpu = bool(self.specs.get("gpu"))
        thread_num = self.specs.get("thread_num", 2)
        alloc_cpus = self.specs.get("alloc_cpus", 2)

        ram_vv = self.specs.get("ram_vv", 1)
        ram_sv = self.specs.get("ram_sv", 1)
        ram_ci = self.specs.get("ram_ci", 4 if has_gpu else 2)

        # manifest_version を処理開始直後に先行保存 (KilledされてもWORK_DIRに残るため次回起動時にスキップ)
        manifest_ver = manifest.get("manifest_version", "")
        if manifest_ver:
            mf_path = os.path.join(work_dir, ".manifest_version")
            try:
                with open(mf_path, "w", encoding="utf-8") as f:
                    f.write(manifest_ver)
                logger.info(f"📌 manifest_version を先行保存しました: {manifest_ver}")
            except Exception as e:
                logger.warning(f"⚠️ manifest_version 保存失敗: {e}")

        logger.info(f"⚙️ [Reconfigure] 構成適用を開始します (work_dir={work_dir}, GPU={has_gpu})")

        # 1. SHAREVOX Dockerfile 生成
        sv_meta = manifest.get("engines", {}).get("sharevox", {})
        sv_dir = os.path.join(work_dir, "docker", "sharevox")
        os.makedirs(sv_dir, exist_ok=True)
        if has_gpu:
            sv_dockerfile = sv_meta.get("dockerfile_gpu", "")
        else:
            sv_dockerfile = sv_meta.get("dockerfile_cpu", "").format(THREAD_NUM=thread_num)
        with open(os.path.join(sv_dir, "Dockerfile"), "w", encoding="utf-8", newline="\n") as f:
            f.write(sv_dockerfile)
        result["steps"].append({"step": "sharevox_dockerfile", "success": True})

        # 2. COEIROINK Dockerfile & entrypoint.sh 生成
        ci_meta = manifest.get("engines", {}).get("coeiroink", {})
        ci_dir = os.path.join(work_dir, "docker", "coeiroink")
        os.makedirs(ci_dir, exist_ok=True)
        if has_gpu:
            ci_dockerfile = ci_meta.get("dockerfile_template", "").format(
                BASE_IMAGE=ci_meta.get("base_image_gpu", "nvidia/cuda:11.8.0-base-ubuntu22.04"),
                COEIROINK_ZIP_URL=ci_meta.get("url_gpu", ""),
                COEIROINK_DIR=ci_meta.get("dir_gpu", "COEIROINK_LINUX_GPU_v.2025.4.1")
            )
        else:
            ci_dockerfile = ci_meta.get("dockerfile_template", "").format(
                BASE_IMAGE=ci_meta.get("base_image_cpu", "ubuntu:22.04"),
                COEIROINK_ZIP_URL=ci_meta.get("url_cpu", ""),
                COEIROINK_DIR=ci_meta.get("dir_cpu", "COEIROINK_LINUX_CPU_v.2025.4.1")
            )
        with open(os.path.join(ci_dir, "Dockerfile"), "w", encoding="utf-8", newline="\n") as f:
            f.write(ci_dockerfile)
        with open(os.path.join(ci_dir, "entrypoint.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write(ci_meta.get("entrypoint_sh", ""))
        try:
            os.chmod(os.path.join(ci_dir, "entrypoint.sh"), 0o755)
        except Exception:
            pass
        result["steps"].append({"step": "coeiroink_dockerfile", "success": True})

        # 3. docker-compose.worker.yml 生成
        if has_gpu:
            vv_image = manifest.get("engines", {}).get("voicevox", {}).get("image_gpu", "voicevox/voicevox_engine:nvidia-ubuntu20.04-latest")
            vv_deploy = (
                "    deploy:\n"
                "      resources:\n"
                "        reservations:\n"
                "          devices:\n"
                "            - driver: nvidia\n"
                "              count: all\n"
                "              capabilities: [gpu]\n"
                f"        limits:\n"
                f"          memory: {ram_vv}G"
            )
            sv_deploy = (
                "    deploy:\n"
                "      resources:\n"
                "        reservations:\n"
                "          devices:\n"
                "            - driver: nvidia\n"
                "              count: all\n"
                "              capabilities: [gpu]\n"
                f"        limits:\n"
                f"          memory: {ram_sv}G"
            )
            ci_deploy = (
                "    deploy:\n"
                "      resources:\n"
                "        reservations:\n"
                "          devices:\n"
                "            - driver: nvidia\n"
                "              count: all\n"
                "              capabilities: [gpu]\n"
                f"        limits:\n"
                f"          memory: {ram_ci}G"
            )
            ci_folder = ci_meta.get("dir_gpu", "COEIROINK_LINUX_GPU_v.2025.4.1")
        else:
            vv_image = manifest.get("engines", {}).get("voicevox", {}).get("image_cpu", "voicevox/voicevox_engine:cpu-ubuntu20.04-latest")
            vv_deploy = (
                "    deploy:\n"
                "      resources:\n"
                "        limits:\n"
                f"          cpus: \"{alloc_cpus}.0\"\n"
                f"          memory: {ram_vv}G"
            )
            sv_deploy = (
                "    deploy:\n"
                "      resources:\n"
                "        limits:\n"
                f"          cpus: \"{alloc_cpus}.0\"\n"
                f"          memory: {ram_sv}G"
            )
            ci_deploy = (
                "    deploy:\n"
                "      resources:\n"
                "        limits:\n"
                f"          cpus: \"{alloc_cpus}.0\"\n"
                f"          memory: {ram_ci}G"
            )
            ci_folder = ci_meta.get("dir_cpu", "COEIROINK_LINUX_CPU_v.2025.4.1")

        compose_content = manifest.get("compose_template", "").format(
            VOICEVOX_IMAGE=vv_image,
            THREAD_NUM=thread_num,
            VV_DEPLOY=vv_deploy,
            SV_DEPLOY=sv_deploy,
            CI_DEPLOY=ci_deploy,
            COEIROINK_DIR=ci_folder
        )

        # ホスト側作業ディレクトリの解決と、COEIROINK ハイブリッドRAMDiskマウントパスの正規化
        # (ヘルパーコンテナ経由起動時に相対パスが /work/coeiroink_models に誤解決され空マウントされる不具合を防止)
        host_work = docker_api.get_host_work_dir() or work_dir
        if len(host_work) >= 2 and host_work[0].isalpha() and host_work[1] == ":":
            norm_work = host_work.replace("\\", "/").rstrip("/")
        elif host_work.startswith("/"):
            norm_work = host_work.rstrip("/")
        else:
            norm_work = "."

        hybrid_mount_src = f"{norm_work}/coeiroink_hybrid"
        ssd_mount_src = f"{norm_work}/coeiroink_models"
        ram_mount_src = f"{norm_work}/coeiroink_ram"

        compose_content = normalize_coeiroink_compose_volumes(compose_content, hybrid_mount_src, ssd_mount_src, ram_mount_src)

        compose_path = os.path.join(work_dir, "docker-compose.worker.yml")
        with open(compose_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(compose_content)
        result["steps"].append({"step": "compose_generate", "success": True, "path": compose_path, "hybrid_mount_src": hybrid_mount_src, "ssd_mount_src": ssd_mount_src, "ram_mount_src": ram_mount_src})

        # 4. COEIROINK モデル同期
        await self.sync_coeiroink_models(work_dir)
        result["steps"].append({"step": "models_sync", "success": True})

        # 既存の同名エンジンコンテナの Conflict を防止するために事前クリーンアップ
        logger.info("🧹 既存のエンジンコンテナをクリーンアップ中 (Conflict防止)...")
        for cname in ["ttsworker-voicevox", "ttsworker-sharevox", "ttsworker-coeiroink"]:
            try:
                docker_api.remove_container(cname, force=True)
            except Exception as e:
                logger.debug(f"クリーンアップスキップ ({cname}): {e}")
        # CLI が利用可能な環境でも事前に同名コンテナを強制排除
        if shutil.which("docker"):
            try:
                subprocess.run(
                    ["docker", "rm", "-f", "ttsworker-voicevox", "ttsworker-sharevox", "ttsworker-coeiroink"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
                )
            except Exception:
                pass

        # 5. docker compose up -d --build (※ --remove-orphans は Agent コンテナが誤削除されるため絶対に使用しない)
        logger.info(f"🚀 docker compose up -d --build を実行します (work_dir={work_dir})...")
        compose_fname = os.path.basename(compose_path)
        has_docker_cli = bool(shutil.which("docker"))

        if has_docker_cli:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose", "-f", compose_path, "up", "-d", "--build",
                    cwd=work_dir,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
                if proc.returncode != 0:
                    err_msg = stderr.decode(errors="replace")[-1000:]
                    logger.error(f"❌ docker compose 実行エラー (rc={proc.returncode}): {err_msg}")
                    result["steps"].append({"step": "compose_up", "success": False, "error": err_msg})
                    return result
                logger.info("✅ docker compose up -d --build 完了！")
                result["steps"].append({"step": "compose_up", "success": True})
            except Exception as e:
                logger.error(f"❌ docker compose 実行例外: {e}")
                result["steps"].append({"step": "compose_up", "success": False, "error": str(e)})
                return result
        else:
            # docker CLI がない場合は docker:cli ヘルパーコンテナを使用
            logger.info("ℹ️ docker CLI 未検出のため、docker:cli ヘルパーコンテナ経由で実行します")
            try:
                code, out = await asyncio.to_thread(
                    docker_api.run_compose_via_helper,
                    work_dir,
                    compose_fname,
                    ["up", "-d", "--build"],
                    False,
                    600
                )
                if code != 0:
                    logger.error(f"❌ ヘルパー経由 docker compose 実行エラー (rc={code}): {out[-1000:]}")
                    result["steps"].append({"step": "compose_up", "success": False, "error": out[-1000:]})
                    return result
                logger.info("✅ ヘルパー経由 docker compose up -d --build 完了！")
                result["steps"].append({"step": "compose_up", "success": True})
            except Exception as e:
                logger.error(f"❌ ヘルパー経由 docker compose 実行例外: {e}")
                result["steps"].append({"step": "compose_up", "success": False, "error": str(e)})
                return result

        # 6. ヘルスチェック待機 (最大90秒)
        logger.info("⏳ 各エンジンの起動完了を待機中...")
        for eng_name, meta in ENGINES.items():
            port = meta["port"]
            health_path = meta.get("health_path", "/")
            url = f"http://127.0.0.1:{port}{health_path}"
            for _ in range(90):
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.0)) as r:
                        if r.status in (200, 404):
                            logger.info(f"  ✅ {eng_name} ({port}) 正常稼働確認")
                            break
                except Exception:
                    pass
                await asyncio.sleep(1.0)

        # 7. Master へ再登録
        await self.register_to_master()

        # 8. マニフェストバージョン確認 (先行保存済みのため確認のみ)
        manifest_ver = manifest.get("manifest_version", "")
        logger.info(f"✅ manifest_version={manifest_ver} の処理が完了しました。")

        result["success"] = True
        logger.info(f"🎉 ノード完全再構成 (manifest_version={manifest_ver}) が完了しました！")
        return result

    async def self_update(self, update_agent: bool = True, update_engines: bool = True, full: bool = False) -> Dict[str, Any]:
        """
        自己アップデートまたは完全再構成を実行する。
        full=True の場合:
          Master からマニフェストを取得し、Dockerfile・Compose・モデル同期・再ビルドを一括自動実行
        """
        async with self._update_lock:
            logger.info(f"🔄 [Self Update] アップデートを開始します (full={full}, update_agent={update_agent})...")
            result: Dict[str, Any] = {"success": False, "steps": []}

            # ===== Step A: Agent 自身のコード更新 =====
            if update_agent:
                session = await self.get_session()
                agent_url = f"{get_master_url()}/static/agent.py"
                try:
                    async with session.get(agent_url, timeout=aiohttp.ClientTimeout(total=15.0)) as resp:
                        if resp.status == 200:
                            new_code = await resp.text()
                            new_version = "unknown"
                            for line in new_code.splitlines():
                                if line.strip().startswith("AGENT_VERSION"):
                                    new_version = line.split('=')[1].strip().strip('"\'')
                                    break

                            if new_version == AGENT_VERSION:
                                step_r = {"step": "agent_download", "skipped": True,
                                          "reason": f"already latest ({AGENT_VERSION})"}
                                result["steps"].append(step_r)
                                logger.info(f"✅ agent.py は最新版です (v{AGENT_VERSION})。")
                                if sys.version_info >= (3, 14):
                                    update_agent = False
                                else:
                                    logger.info(f"ℹ️ 現在の Python は {sys.version.split()[0]} です。Python 3.14 へのコンテナ再作成を継続します。")
                            else:
                                tmp_path = AGENT_SCRIPT_PATH + ".new"
                                with open(tmp_path, "w", encoding="utf-8") as f:
                                    f.write(new_code)
                                os.replace(tmp_path, AGENT_SCRIPT_PATH)
                                step_r = {"step": "agent_download", "success": True,
                                          "old_version": AGENT_VERSION, "new_version": new_version}
                                result["steps"].append(step_r)
                                logger.info(f"✅ agent.py を v{AGENT_VERSION} → v{new_version} に更新しました。")
                        else:
                            step_r = {"step": "agent_download", "success": False, "status": resp.status}
                            result["steps"].append(step_r)
                            logger.warning(f"⚠️ agent.py の取得に失敗しました (HTTP {resp.status})。")
                            update_agent = False
                except Exception as e:
                    result["steps"].append({"step": "agent_download", "error": str(e)})
                    logger.error(f"❌ agent.py ダウンロードエラー: {e}")
                    update_agent = False

            # ===== Step B: 完全再構成 (full=True) =====
            if full:
                recon_res = await self.reconfigure_stack()
                result["steps"].extend(recon_res.get("steps", []))
                result["success"] = recon_res.get("success", False)
                if not result["success"]:
                    result["error"] = recon_res.get("error", "Reconfiguration failed")

            # ===== Step C: 通常の engines 更新 (full=False かつ update_engines=True) =====
            elif update_engines:
                compose_file = WORKER_COMPOSE_FILE
                if not os.path.exists(compose_file):
                    msg = f"compose ファイルが見つかりません: {compose_file}"
                    logger.warning(f"⚠️ {msg}")
                    result["steps"].append({"step": "engines", "skipped": True, "reason": msg})
                else:
                    has_docker_cli = bool(shutil.which("docker"))
                    compose_fname = os.path.basename(compose_file)
                    work_dir = resolve_work_dir()

                    if has_docker_cli:
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "docker", "compose", "-f", compose_file, "pull",
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                            )
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
                            result["steps"].append({
                                "step": "engines_pull", "returncode": proc.returncode,
                                "stderr": stderr.decode(errors="replace")[-800:]
                            })
                            logger.info(f"✅ docker compose pull 完了 (rc={proc.returncode})")
                        except asyncio.TimeoutError:
                            result["steps"].append({"step": "engines_pull", "error": "timeout"})

                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "docker", "compose", "-f", compose_file, "up", "-d",
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                            )
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                            result["steps"].append({
                                "step": "engines_up", "returncode": proc.returncode,
                                "stderr": stderr.decode(errors="replace")[-800:]
                            })
                            logger.info(f"✅ docker compose up -d 完了 (rc={proc.returncode})")
                            result["success"] = proc.returncode == 0
                        except asyncio.TimeoutError:
                            result["steps"].append({"step": "engines_up", "error": "timeout"})
                    elif docker_api.is_available():
                        logger.info("ℹ️ docker CLI 未検出のため、docker:cli ヘルパーコンテナ経由で engines を更新します")
                        try:
                            code, out = await asyncio.to_thread(
                                docker_api.run_compose_via_helper,
                                work_dir,
                                compose_fname,
                                ["pull"],
                                False,
                                300
                            )
                            result["steps"].append({"step": "engines_pull", "returncode": code, "stderr": out[-800:]})
                            logger.info(f"✅ ヘルパー経由 docker compose pull 完了 (rc={code})")
                        except Exception as e:
                            result["steps"].append({"step": "engines_pull", "error": str(e)})

                        try:
                            code, out = await asyncio.to_thread(
                                docker_api.run_compose_via_helper,
                                work_dir,
                                compose_fname,
                                ["up", "-d"],
                                False,
                                120
                            )
                            result["steps"].append({"step": "engines_up", "returncode": code, "stderr": out[-800:]})
                            logger.info(f"✅ ヘルパー経由 docker compose up -d 完了 (rc={code})")
                            result["success"] = (code == 0)
                        except Exception as e:
                            result["steps"].append({"step": "engines_up", "error": str(e)})
                    else:
                        logger.error("❌ docker CLI も docker.sock も利用できません。")
                        result["steps"].append({"step": "engines", "error": "docker unavailable"})

            if not update_agent and not update_engines and not full:
                result["success"] = True

            # Agent コンテナの再起動 (ファイルを更新した場合のみ最後に実施)
            # docker restart だけではイメージが変わらないため、
            # docker compose pull + up -d でコンテナを再作成する
            if update_agent:
                try:
                    work_dir = resolve_work_dir()
                    agent_compose = AGENT_COMPOSE_FILE
                    # フォールバック: WORK_DIR 配下の docker-compose.agent.yml を検索
                    if not os.path.exists(agent_compose):
                        fallback = os.path.join(work_dir, "docker-compose.agent.yml")
                        if os.path.exists(fallback):
                            agent_compose = fallback
                        else:
                            # 存在しない場合は標準構成で自動生成
                            try:
                                minimal_agent_compose = (
                                    "services:\n"
                                    "  worker-agent:\n"
                                    "    image: python:3.14-slim\n"
                                    "    container_name: ttsworker-agent\n"
                                    "    restart: unless-stopped\n"
                                    "    network_mode: host\n"
                                    "    volumes:\n"
                                    "      - /var/run/docker.sock:/var/run/docker.sock\n"
                                    "      - ./:/work\n"
                                    "      - ./docker/agent:/app\n"
                                    "      - ./coeiroink_models:/coeiroink_models\n"
                                    "      - ./docker-compose.worker.yml:/app/docker-compose.worker.yml\n"
                                    "    working_dir: /app\n"
                                    "    environment:\n"
                                    f"      - MASTER_URL={get_master_url()}\n"
                                    f"      - NODE_ID={NODE_ID}\n"
                                    "      - AGENT_PORT=50030\n"
                                    "      - WORK_DIR=/work\n"
                                    "      - WORKER_COMPOSE_FILE=/work/docker-compose.worker.yml\n"
                                    "    command: >\n"
                                    "      sh -c \"pip install --quiet --no-cache-dir --root-user-action=ignore aiohttp && python3 agent.py\"\n"
                                )
                                with open(fallback, "w", encoding="utf-8") as f:
                                    f.write(minimal_agent_compose)
                                agent_compose = fallback
                                logger.info(f"📝 {fallback} を自動生成しました。")
                            except Exception as e:
                                logger.warning(f"⚠️ agent compose 自動生成失敗: {e}")

                    # 1) docker-compose.agent.yml のイメージ指定を python:3.14-slim に自動更新
                    if os.path.exists(agent_compose):
                        ensure_agent_compose_python314(agent_compose)

                    # 2) コンテナ再作成または Graceful Restart の実行
                    agent_container = os.getenv("AGENT_CONTAINER_NAME", "ttsworker-agent")
                    compose_fname = os.path.basename(agent_compose)

                    # すでに Python 3.14 環境で動作している場合:
                    # コンテナイメージの切り替えは不要であり、更新された agent.py はすでにマウント先に保存済み。
                    # ヘルパーコンテナや Compose 起動を行わず、コンテナ自身を Graceful Restart して
                    # 100% 確実に最新コードで再起動する (Docker の restart: unless-stopped により自動再開)
                    if sys.version_info >= (3, 14):
                        logger.info("🎉 [Agent Self-Update] Python 3.14 稼働中: コンテナを Graceful Restart して最新 agent.py を反映します")
                        async def _restart_self():
                            await asyncio.sleep(1.0)
                            if docker_api.is_available():
                                try:
                                    docker_api.restart_container(agent_container, 2)
                                    return
                                except Exception:
                                    pass
                            # フォールバック: プロセス終了 (restart ポリシーで自動再起動)
                            os._exit(0)

                        asyncio.create_task(_restart_self())
                        result["steps"].append({"step": "agent_restart", "success": True, "method": "graceful_restart"})
                        result["success"] = True
                        return result

                    async def _recreate_self(compose_path=agent_compose, wd=work_dir, cfname=compose_fname, cname=agent_container):
                        await asyncio.sleep(1.5)
                        has_docker_cli = bool(shutil.which("docker"))

                        if has_docker_cli and os.path.exists(compose_path):
                            logger.info(f"🔄 [Agent Self-Update] 直接 docker compose pull & up -d を実行します")
                            try:
                                pull_proc = await asyncio.create_subprocess_exec(
                                    "docker", "compose", "-f", compose_path, "pull",
                                    cwd=wd,
                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                                )
                                await asyncio.wait_for(pull_proc.communicate(), timeout=180)
                                await asyncio.create_subprocess_exec(
                                    "docker", "compose", "-f", compose_path, "up", "-d",
                                    cwd=wd,
                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                                )
                                return
                            except Exception as e:
                                logger.warning(f"⚠️ 直接 docker compose 失敗、ヘルパーコンテナを試行: {e}")

                        # docker CLI がない場合、または直接実行失敗時はヘルパーコンテナ方式
                        if docker_api.is_available() and os.path.exists(compose_path):
                            logger.info(f"🔄 [Agent Self-Update] docker:cli ヘルパー経由で pull & up -d を実行します")
                            try:
                                code, out = await asyncio.to_thread(
                                    docker_api.run_compose_via_helper, wd, cfname, ["pull"], False, 180
                                )
                                logger.info(f"🔄 [Agent Self-Update] pull 完了 (code={code})")
                                code, out = await asyncio.to_thread(
                                    docker_api.run_compose_via_helper, wd, cfname, ["up", "-d"], True, 60
                                )
                                logger.info(f"🔄 [Agent Self-Update] up -d をバックグラウンド起動しました (code={code})")
                                return
                            except Exception as e:
                                logger.warning(f"⚠️ ヘルパーコンテナ実行失敗: {e}")

                        # 最終フォールバック: コンテナ再起動
                        logger.warning("⚠️ compose による再作成が利用できないため、コンテナ再起動にフォールバックします")
                        if docker_api.is_available():
                            await asyncio.to_thread(docker_api.restart_container, cname, 5)
                        elif has_docker_cli:
                            subprocess.run(["docker", "restart", cname], timeout=30)

                    asyncio.create_task(_recreate_self())
                    result["steps"].append({"step": "agent_recreate", "scheduled": True,
                                            "compose": agent_compose})
                    logger.info(f"🔄 Agent コンテナを 1.5秒後に pull + 再作成します (compose={agent_compose})")
                except Exception as e:
                    result["steps"].append({"step": "agent_restart", "error": str(e)})
                    logger.error(f"❌ Agent 再起動に失敗: {e}")

            return result

    async def auto_version_check_loop(self):
        """
        定期的に Master の manifest & agent.py バージョンをチェックし、新版があれば自動アップデート。
        """
        await asyncio.sleep(120)
        logger.info(f"🔍 [AutoUpdate] 定期構成チェックを開始します (間隔={AUTO_UPDATE_CHECK_INTERVAL}分)")

        while True:
            # Master がハートビートで更新を調停している場合は、全台同時更新を避けるため本ループでは更新しない
            if getattr(self, "_master_coordinates_updates", False):
                logger.debug("[AutoUpdate] Master 調停モードのため定期チェックをスキップします。")
                await asyncio.sleep(AUTO_UPDATE_CHECK_INTERVAL * 60)
                continue
            try:
                session = await self.get_session()
                manifest_url = f"{get_master_url()}/api/worker/manifest"
                async with session.get(manifest_url, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                    if resp.status == 200:
                        manifest = await resp.json()
                        remote_manifest_ver = manifest.get("manifest_version")
                        remote_agent_ver = manifest.get("agent_version")

                        work_dir = resolve_work_dir()
                        mf_path = os.path.join(work_dir, ".manifest_version")
                        local_manifest_ver = ""
                        if os.path.exists(mf_path):
                            try:
                                with open(mf_path, "r", encoding="utf-8") as f:
                                    local_manifest_ver = f.read().strip()
                            except Exception:
                                pass

                        def _is_newer(remote: str, local: str) -> bool:
                            try:
                                r_parts = [int(x) for x in remote.split(".")]
                                l_parts = [int(x) for x in local.split(".")]
                                return r_parts > l_parts
                            except Exception:
                                return False

                        need_reconfigure = bool(remote_manifest_ver and remote_manifest_ver != local_manifest_ver)
                        need_agent_update = bool(remote_agent_ver and _is_newer(remote_agent_ver, AGENT_VERSION)) or (sys.version_info < (3, 14))

                        if need_agent_update or need_reconfigure:
                            logger.info(
                                f"🆕 [AutoUpdate] 構成更新を検出！ (Manifest: {local_manifest_ver} → {remote_manifest_ver}, Agent: {AGENT_VERSION} → {remote_agent_ver})"
                            )
                            if not self._update_lock.locked():
                                asyncio.create_task(
                                    self.self_update(
                                        update_agent=need_agent_update,
                                        update_engines=True,
                                        full=need_reconfigure
                                    )
                                )
                        else:
                            logger.debug(f"[AutoUpdate] 最新構成です (Manifest={local_manifest_ver}, Agent=v{AGENT_VERSION})")
                            # 定期監査: エンジンスタックが欠損していないか確認・自動修復
                            try:
                                await self.ensure_engine_stack_exists()
                            except Exception as e_st:
                                logger.debug(f"[AutoUpdate] スタック定期監査例外: {e_st}")
            except Exception as e:
                logger.debug(f"[AutoUpdate] バージョンチェック失敗: {e}")

            await asyncio.sleep(AUTO_UPDATE_CHECK_INTERVAL * 60)



# --- Web API ハンドラー ---

async def handle_engine_start(request: web.Request) -> web.Response:
    agent: WorkerAgent = request.app["agent"]
    eng = request.query.get("engine", "").lower()
    if not eng:
        try:
            body = await request.json()
            eng = body.get("engine", "").lower()
        except Exception:
            pass
    if eng not in ENGINES:
        return web.json_response({"error": f"Unknown engine: {eng}"}, status=400)
    ok = await agent.start_engine(eng)
    return web.json_response({"success": ok, "engine": eng})


async def handle_engine_stop(request: web.Request) -> web.Response:
    agent: WorkerAgent = request.app["agent"]
    eng = request.query.get("engine", "").lower()
    if not eng:
        try:
            body = await request.json()
            eng = body.get("engine", "").lower()
        except Exception:
            pass
    if eng == "all" or not eng:
        results = {}
        for e in ENGINES:
            results[e] = agent.stop_engine(e)
        logger.info(f"🛑 [Remote Stop] Master requested stopping all engines: {results}")
        return web.json_response({"success": True, "engines": results})

    if eng not in ENGINES:
        return web.json_response({"error": f"Unknown engine: {eng}"}, status=400)
    ok = agent.stop_engine(eng)
    return web.json_response({"success": ok, "engine": eng})


async def handle_status(request: web.Request) -> web.Response:
    agent: WorkerAgent = request.app["agent"]
    now = time.time()

    # リソース情報は最大 5秒キャッシュ (頻繁な nvidia-smi 呼び出しを防ぐ)
    if now - agent._last_resource_time > 5.0:
        agent._last_resource_usage = await asyncio.to_thread(get_resource_usage)
        agent._last_resource_time = now

    work_dir = resolve_work_dir()
    mf_path = os.path.join(work_dir, ".manifest_version")
    local_manifest_ver = ""
    if os.path.exists(mf_path):
        try:
            with open(mf_path, "r", encoding="utf-8") as f:
                local_manifest_ver = f.read().strip()
        except Exception:
            pass

    res = {
        "node_id": agent.node_id,
        "ip": agent.local_ip,
        "master_registered": agent.registered_to_master,
        "agent_version": AGENT_VERSION,
        "manifest_version": local_manifest_ver,
        "specs": agent.specs,
        "resources": agent._last_resource_usage,
        "engines": {
            eng: {
                "running": is_container_running(meta["container_name"]),
                "port": meta["port"],
                "last_used_seconds_ago": round(now - agent.last_used_time.get(eng, 0)) if agent.last_used_time.get(eng, 0) > 0 else None
            }
            for eng, meta in ENGINES.items()
        },
        "ram_status": agent.ram_manager.get_status() if agent.ram_manager else None
    }
    return web.json_response(res)


async def handle_update(request: web.Request) -> web.Response:
    """Master または管理者からの自己アップデート・完全再構成トリガー"""
    agent: WorkerAgent = request.app["agent"]
    if agent._update_lock.locked():
        return web.json_response({"success": False, "message": "アップデートが既に実行中です。"}, status=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    update_agent   = bool(body.get("update_agent",   True))
    update_engines = bool(body.get("update_engines", True))
    full           = bool(body.get("full",           False))

    asyncio.create_task(agent.self_update(update_agent=update_agent, update_engines=update_engines, full=full))
    return web.json_response({
        "success": True,
        "message": "完全再構成・アップデートを開始しました。" if full else "アップデートを開始しました。",
        "current_agent_version": AGENT_VERSION,
        "update_agent": update_agent,
        "update_engines": update_engines,
        "full": full,
    })


async def handle_logs(request: web.Request) -> web.Response:
    """直近の Worker ログおよび各エンジンコンテナのログを返却 (デバッグ・稼働監視用)"""
    lines = int(request.query.get("lines", request.query.get("tail", "100")))
    target = request.query.get("target", "all").lower()

    res_data: Dict[str, Any] = {
        "node_id": NODE_ID,
        "agent_version": AGENT_VERSION,
        "python_version": sys.version,
        "target": target,
        "timestamp": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 1. Agent 自身のログ
    agent_logs = list(_LOG_BUFFER)[-lines:]
    if target in ("agent", "all"):
        res_data["agent_logs"] = agent_logs
        if target == "agent":
            res_data["logs"] = agent_logs
            res_data["log_count"] = len(agent_logs)
            return web.json_response(res_data)

    # 2. 各エンジンコンテナのログ
    engine_logs = {}
    for eng_name, eng_info in ENGINES.items():
        if target != "all" and target != eng_name:
            continue
        cname = eng_info.get("container_name")
        if not cname:
            continue

        if docker_api.is_available():
            running = docker_api.is_running(cname)
            if running:
                raw_logs = docker_api.get_container_logs(cname, tail=lines)
                engine_logs[eng_name] = {
                    "container_name": cname,
                    "running": True,
                    "logs": raw_logs.splitlines() if isinstance(raw_logs, str) else []
                }
            else:
                engine_logs[eng_name] = {
                    "container_name": cname,
                    "running": False,
                    "logs": ["(Container is stopped or idle)"]
                }
        else:
            engine_logs[eng_name] = {
                "container_name": cname,
                "running": False,
                "logs": ["(Docker API not available)"]
            }

    if target in ENGINES:
        eng_res = engine_logs.get(target, {})
        res_data["logs"] = eng_res.get("logs", [])
        res_data["log_count"] = len(res_data["logs"])
        res_data["running"] = eng_res.get("running", False)
        return web.json_response(res_data)

    # target == "all"
    res_data["engines"] = engine_logs
    res_data["logs"] = agent_logs  # 後方互換性
    res_data["log_count"] = len(agent_logs)
    return web.json_response(res_data)


async def handle_health(request: web.Request) -> web.Response:
    """シンプルな生存確認エンドポイント (updating: 更新処理中か / Master のローリング更新完了判定に使用)"""
    agent: WorkerAgent = request.app["agent"]
    return web.json_response({
        "status": "ok",
        "node_id": NODE_ID,
        "agent_version": AGENT_VERSION,
        "manifest_version": read_local_manifest_version(),
        "updating": agent._update_lock.locked(),
        "started_at": AGENT_STARTED_AT,
    })


async def handle_restart(request: web.Request) -> web.Response:
    """稼働中の TTS エンジンコンテナと Agent コンテナ自身を再起動する (レスポンス返却後に実行)"""
    agent: WorkerAgent = request.app["agent"]
    if agent._update_lock.locked():
        return web.json_response({"success": False, "message": "アップデート実行中のため再起動できません。"}, status=409)
    running = [meta["container_name"] for meta in ENGINES.values() if is_container_running(meta["container_name"])]
    agent_container = os.getenv("AGENT_CONTAINER_NAME", "ttsworker-agent")

    async def _do_restart():
        async with agent._update_lock:
            await asyncio.sleep(1.0)
            for cname in running:
                logger.info(f"🔄 [Remote Restart] エンジンコンテナを再起動します: {cname}")
                code, out = await asyncio.to_thread(run_docker_cmd, "restart", "-t", "5", cname)
                if code != 0:
                    logger.warning(f"⚠️ [Remote Restart] {cname} の再起動に失敗 (code={code}): {str(out)[-300:]}")
            logger.info(f"🔄 [Remote Restart] Agent コンテナを再起動します: {agent_container}")
        # 自コンテナ再起動 (このプロセスはここで終了する)
        await asyncio.to_thread(run_docker_cmd, "restart", "-t", "5", agent_container)

    asyncio.create_task(_do_restart())
    return web.json_response({"success": True, "engines": running, "agent_container": agent_container,
                              "message": "エンジンとAgentの再起動を開始しました。"})


async def handle_diagnostics(request: web.Request) -> web.Response:
    """ノード内の詳細診断情報（コンテナマウント、モデルディレクトリ、コンテナ内ファイル）を返却"""
    agent: WorkerAgent = request.app["agent"]
    work_dir = resolve_work_dir()
    models_dir = os.path.join(work_dir, "coeiroink_models")

    # 1. ホスト側 models ディレクトリの調査 (高速)
    models_on_disk = []
    if os.path.exists(models_dir):
        try:
            for item in sorted(os.listdir(models_dir)):
                ipath = os.path.join(models_dir, item)
                if os.path.isdir(ipath):
                    has_metas = os.path.exists(os.path.join(ipath, "metas.json"))
                    models_on_disk.append({"folder": item, "has_metas": has_metas})
        except Exception as e:
            models_on_disk.append({"error": str(e)})

    # 2. コンテナの inspect
    ci_data = docker_api.inspect_container("ttsworker-coeiroink") or {}
    mounts = ci_data.get("Mounts", [])
    env = ci_data.get("Config", {}).get("Env", [])
    ag_data = docker_api.inspect_container("ttsworker-agent") or {}
    ag_mounts = ag_data.get("Mounts", [])

    # 3. コンテナ内でのコマンド実行 (オプショナル)
    out_ls_opt = "skipped"
    out_ls_spk = "skipped"
    out_find = "skipped"
    if request.query.get("exec", "0") in ("1", "true"):
        _, out_ls_opt = docker_api.exec_cmd("ttsworker-coeiroink", ["ls", "-la", "/opt/coeiroink"], timeout=5)
        _, out_ls_spk = docker_api.exec_cmd("ttsworker-coeiroink", ["sh", "-c", "ls -la /opt/coeiroink/*/speaker_info 2>&1 || true"], timeout=5)
        _, out_find = docker_api.exec_cmd("ttsworker-coeiroink", ["sh", "-c", "find /opt -name metas.json 2>&1 || true"], timeout=5)

    return web.json_response({
        "node_id": agent.node_id,
        "work_dir": work_dir,
        "host_work_dir": docker_api.get_host_work_dir(),
        "models_dir_path": models_dir,
        "models_dir_exists": os.path.exists(models_dir),
        "models_on_disk_count": len(models_on_disk),
        "models_on_disk": models_on_disk,
        "container_running": docker_api.is_running("ttsworker-coeiroink"),
        "container_mounts": mounts,
        "agent_mounts": ag_mounts,
        "container_env": [e for e in env if "TOKEN" not in e and "KEY" not in e],
        "container_ls_opt": out_ls_opt,
        "container_ls_speaker_info": out_ls_spk,
        "container_find_metas": out_find,
        "container_logs": docker_api.get_container_logs("ttsworker-coeiroink", tail=30)
    })


async def handle_sync_models(request: web.Request) -> web.Response:
    """手動またはMasterからモデル同期のみを即座に実行"""
    agent: WorkerAgent = request.app["agent"]
    work_dir = resolve_work_dir()
    clean_model = request.query.get("clean_model")
    if clean_model:
        target_dir = os.path.join(work_dir, "coeiroink_models", clean_model)
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
            logger.info(f"🗑️ 指定モデル '{clean_model}' をクリーン削除しました: {target_dir}")
    asyncio.create_task(agent.sync_coeiroink_models(work_dir))
    return web.json_response({"success": True, "message": f"COEIROINK モデル同期を開始しました (clean_model={clean_model})。"})


async def handle_fix_coeiroink(request: web.Request) -> web.Response:
    """COEIROINK のバインドマウント元を即時修復して再作成"""
    agent: WorkerAgent = request.app["agent"]
    res = await agent.fix_coeiroink_mount()
    return web.json_response(res)


async def handle_ram_promote(request: web.Request) -> web.Response:
    """指定モデルをRAMDiskへ昇格 (Pre-warm / ピン留め)"""
    agent: WorkerAgent = request.app["agent"]
    if not agent.ram_manager:
        return web.json_response({"success": False, "error": "RamManager not initialized"}, status=500)
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = data.get("model") or request.query.get("model")
    pinned = bool(data.get("pinned", False))
    if not model:
        return web.json_response({"error": "model is required"}, status=400)
    if pinned:
        agent.ram_manager.pin_model(model)
    ok = await agent.ram_manager.promote_model(model)
    return web.json_response({"success": ok, "model": model, "pinned": pinned})


async def handle_ram_status(request: web.Request) -> web.Response:
    """RAMDisk状態サマリーの取得"""
    agent: WorkerAgent = request.app["agent"]
    if not agent.ram_manager:
        return web.json_response({"success": False, "error": "RamManager not initialized"}, status=500)
    status = agent.ram_manager.get_status()
    return web.json_response(status)


async def handle_ram_demote(request: web.Request) -> web.Response:
    """指定モデルのRAMDisk解放"""
    agent: WorkerAgent = request.app["agent"]
    if not agent.ram_manager:
        return web.json_response({"success": False, "error": "RamManager not initialized"}, status=500)
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = data.get("model") or request.query.get("model")
    force = bool(data.get("force", False))
    if not model:
        return web.json_response({"error": "model is required"}, status=400)
    ok = await agent.ram_manager.demote_model(model, force=force)
    return web.json_response({"success": ok, "model": model})


async def handle_ram_flush(request: web.Request) -> web.Response:
    """緊急全解放のトリガー"""
    agent: WorkerAgent = request.app["agent"]
    if not agent.ram_manager:
        return web.json_response({"success": False, "error": "RamManager not initialized"}, status=500)
    await agent.ram_manager.emergency_flush()
    return web.json_response({"success": True, "message": "Emergency flush triggered"})


async def main():
    agent = WorkerAgent()
    agent.start_rsync_daemon()

    app = web.Application()
    app["agent"] = agent
    app.router.add_post("/engine/start", handle_engine_start)
    app.router.add_post("/engine/stop", handle_engine_stop)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/update", handle_update)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/restart", handle_restart)
    app.router.add_get("/logs", handle_logs)
    app.router.add_get("/diagnostics", handle_diagnostics)
    app.router.add_post("/models/sync", handle_sync_models)
    app.router.add_post("/engine/fix_coeiroink", handle_fix_coeiroink)
    app.router.add_post("/models/ram_promote", handle_ram_promote)
    app.router.add_get("/models/ram_status", handle_ram_status)
    app.router.add_post("/models/ram_demote", handle_ram_demote)
    app.router.add_post("/models/ram_flush", handle_ram_flush)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", AGENT_PORT)

    # ポートバインド (一時的なソケット競合やTIME_WAITに備えて自動リトライ)
    max_bind_retries = 10
    bind_retry_interval = 3.0
    for attempt in range(1, max_bind_retries + 1):
        try:
            await site.start()
            break
        except OSError as e:
            if getattr(e, "errno", None) == 98 or "address already in use" in str(e).lower():
                if attempt < max_bind_retries:
                    logger.warning(
                        f"⚠️ [PortConflict] ポート {AGENT_PORT} が既に使用されています "
                        f"(試行 {attempt}/{max_bind_retries})。{bind_retry_interval:.1f}秒後に再試行します... "
                        f"(ヒント: ホスト側で 'lsof -i :{AGENT_PORT}' または 'ss -tanp | grep {AGENT_PORT}' を確認してください)"
                    )
                    await asyncio.sleep(bind_retry_interval)
                    continue
                else:
                    logger.error(
                        f"❌ [PortConflict] ポート {AGENT_PORT} のバインドに {max_bind_retries} 回失敗しました。詳細: {e}"
                    )
            raise

    logger.info(f"🚀 Worker Agent API サーバー起動: http://0.0.0.0:{AGENT_PORT}  node_id={NODE_ID}")

    # バックグラウンドループ開始 (heartbeat_loop内で初回登録も行う)
    asyncio.create_task(agent.auto_idle_monitor())
    asyncio.create_task(agent.heartbeat_loop())
    asyncio.create_task(agent.engine_watchdog_loop())
    asyncio.create_task(agent.auto_version_check_loop())
    asyncio.create_task(agent.ensure_coeiroink_mount_healthy())
    # 起動時スタック保証 (欠落コンテナがあればComposeから自動復元)
    asyncio.create_task(agent.ensure_engine_stack_exists())

    # 永続待機
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
