#!/usr/bin/env python3
"""
TTSBot Volunteer Worker Agent
ボランティア型分散音声合成ワーカークライアント

【概要】
- ポート開放不要: クライアントPCからサーバー (WSS) へアウトバウンド接続
- 自動エンジン検出: ローカルで起動している VOICEVOX / COEIROINK / SHAREVOX を自動検出
- ゼロコピーバイナリ転送: 高速なバイナリWebSocketフレームで音声を返送
- 自動再接続: 切断時の指数バックオフリトライ
- 【Dockerモード】: アイドル時 (10分) の自動スリープ (docker stop) & オンデマンド復帰 (docker start) & 終了時クリーンアップ
- 【ネイティブモード】: ユーザーがローカルエンジンを閉じた場合にエージェントも安全に自動終了

【実行例】
python volunteer_worker.py --server-url wss://tts.example.com/api/worker/ws --token YOUR_TOKEN --name "MyGamingPC" --runtime docker
"""

import os
import sys
import time
import socket
import struct
import platform
import argparse
import asyncio
import logging
import subprocess
import urllib.parse
import re
from typing import Dict, Any, List, Optional, Set

try:
    import aiohttp
except ImportError:
    print("❌ [Error] 'aiohttp' がインストールされていません。")
    print("   pip install aiohttp を実行してください。")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("VolunteerWorker")

# プロトコル定数
PROTOCOL_VERSION = "2.3.0"
BINARY_HEADER_FORMAT = ">32sIB"
FLAG_FINAL_CHUNK = 0x01

DOCKER_CONTAINER_MAP = {
    "voicevox": "ttsbot-worker-voicevox",
    "sharevox": "ttsbot-worker-sharevox",
    "coeiroink": "ttsbot-worker-coeiroink",
}

def load_or_create_device_id() -> str:
    """端末 ID (初回生成してホーム配下に保存)。Master はこれでノード ID を安定化し、端末と所有者を紐づける"""
    path = os.path.join(os.path.expanduser("~"), ".ttsbot-volunteer", "device_id")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                return saved
        import uuid
        new_id = uuid.uuid4().hex
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_id)
        return new_id
    except Exception:
        return ""


def pack_binary_chunk(req_id: str, chunk_index: int, payload: bytes, is_final: bool = True) -> bytes:
    req_id_bytes = req_id.encode('ascii')[:32].ljust(32, b' ')
    flags = FLAG_FINAL_CHUNK if is_final else 0x00
    header = struct.pack(BINARY_HEADER_FORMAT, req_id_bytes, chunk_index, flags)
    return header + payload

class VolunteerWorker:
    def __init__(self, args: argparse.Namespace):
        self.server_url = args.server_url
        self.token = args.token
        self.node_name = args.name or socket.gethostname()
        self.node_id = f"volunteer_{self.node_name.lower().replace(' ', '_')}_{int(time.time()) % 10000}"
        self.device_id = load_or_create_device_id()
        self.capacity = args.capacity
        self.priority = args.priority
        self.exclusive_guild_id = getattr(args, "exclusive_guild_id", "") or None
        self.game_priority = getattr(args, "game_priority", False)
        self.runtime = getattr(args, "runtime", "native").lower()
        self.idle_timeout = getattr(args, "idle_timeout", 600)  # 10分 (600秒)
        self.idle_policy: Optional[Dict[str, Any]] = None  # 所有者が WebUI で設定した自動停止 (未配信なら --idle-timeout)
        self.auto_exit = not getattr(args, "no_auto_exit", False)
        
        self.engine_urls = {
            "voicevox": args.voicevox_url.rstrip("/"),
            "coeiroink": args.coeiroink_url.rstrip("/"),
            "sharevox": args.sharevox_url.rstrip("/"),
        }
        self.supported_engines: List[str] = []
        self.detected_ports: Set[int] = set()
        self.session: Optional[aiohttp.ClientSession] = None
        self.active_tasks = 0
        self.start_time = time.time()
        self.last_activity_time = time.time()
        self._bg_tasks: List[asyncio.Task] = []
        self.supported_models: Dict[str, List[str]] = {}

        if self.game_priority:
            self._apply_low_process_priority()

    def _apply_low_process_priority(self):
        """ゲームプレイへの影響を避けるためプロセス優先度を通常以下(BelowNormal)に設定"""
        try:
            if platform.system() == "Windows":
                import ctypes
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000)
                logger.info("🎮 [GameMode] プロセス優先度を '通常以下 (BelowNormal)' に設定しました (ゲームFPS優先)")
            else:
                os.nice(10)
                logger.info("🎮 [GameMode] プロセス nice 値を +10 に設定しました (PC負荷軽減)")
        except Exception as e:
            logger.debug(f"優先度設定スキップ: {e}")

    # ==========================================
    # Docker 管理ヘルパー
    # ==========================================
    def _is_container_running(self, container_name: str) -> bool:
        try:
            out = subprocess.check_output(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                stderr=subprocess.DEVNULL, timeout=3.0
            ).decode().strip()
            return out.lower() == "true"
        except Exception:
            return False

    def _start_docker_container(self, container_name: str) -> bool:
        try:
            logger.info(f"⚡ [Docker] コンテナ '{container_name}' をオンデマンド起動中...")
            subprocess.run(["docker", "start", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0)
            return True
        except Exception as e:
            logger.error(f"❌ コンテナ {container_name} 起動エラー: {e}")
            return False

    def _stop_docker_container(self, container_name: str) -> bool:
        try:
            logger.info(f"💤 [Docker] コンテナ '{container_name}' を停止 (スリープ/解放) 中...")
            subprocess.run(["docker", "stop", "-t", "5", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0)
            return True
        except Exception as e:
            logger.error(f"❌ コンテナ {container_name} 停止エラー: {e}")
            return False

    async def _is_port_open(self, host: str, port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.8)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _ensure_engine_ready(self, engine: str):
        """リクエスト受信時に Docker コンテナが停止していればオンデマンドで即座に復帰"""
        self.last_activity_time = time.time()
        if self.runtime != "docker":
            return

        cname = DOCKER_CONTAINER_MAP.get(engine)
        if not cname:
            return

        if not self._is_container_running(cname):
            self._start_docker_container(cname)
            target_port = 50021 if engine == "voicevox" else (50025 if engine == "sharevox" else 50031)
            for _ in range(30):
                if await self._is_port_open("127.0.0.1", target_port):
                    logger.info(f"✨ [Docker] エンジン '{engine}' の準備が完了しました")
                    return
                await asyncio.sleep(0.5)
            logger.warning(f"⚠️ [Docker] エンジン '{engine}' のポート応答タイムアウト")

    # ==========================================
    # エンジン検出 & スペック収集
    # ==========================================
    async def detect_local_engines(self) -> List[str]:
        """ローカルで稼働している各音声合成エンジンをスマートAPI判定"""
        available = set()
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            candidate_ports = [50021, 50025, 50031, 50032]
            for u in self.engine_urls.values():
                try:
                    p = int(u.split(":")[-1].split("/")[0])
                    if p not in candidate_ports:
                        candidate_ports.append(p)
                except Exception:
                    pass

            for port in candidate_ports:
                base = f"http://127.0.0.1:{port}"
                # 1. engine_manifest による判定
                try:
                    async with s.get(f"{base}/engine_manifest") as r:
                        if r.status == 200:
                            data = await r.json()
                            brand = str(data.get("brand_name") or data.get("name") or "").upper()
                            if "SHAREVOX" in brand:
                                logger.info(f"✨ SHAREVOX 検出 (manifest判定): {base}")
                                self.engine_urls["sharevox"] = base
                                available.add("sharevox")
                                self.detected_ports.add(port)
                                continue
                            elif "VOICEVOX" in brand:
                                logger.info(f"✨ VOICEVOX 検出 (manifest判定): {base}")
                                self.engine_urls["voicevox"] = base
                                available.add("voicevox")
                                self.detected_ports.add(port)
                                continue
                except Exception:
                    pass

                # 2. COEIROINK v1/v2 判定
                try:
                    async with s.get(f"{base}/v1/speakers") as r:
                        if r.status == 200:
                            logger.info(f"✨ COEIROINK 検出 (API判定): {base}")
                            self.engine_urls["coeiroink"] = base
                            available.add("coeiroink")
                            self.detected_ports.add(port)
                            continue
                except Exception:
                    pass

                # 3. /speakers による話者名判定フォールバック
                try:
                    async with s.get(f"{base}/speakers") as r:
                        if r.status == 200:
                            text = await r.text()
                            if "小春音アミ" in text or "SHAREVOX" in text:
                                logger.info(f"✨ SHAREVOX 検出 (話者リスト判定): {base}")
                                self.engine_urls["sharevox"] = base
                                available.add("sharevox")
                                self.detected_ports.add(port)
                            else:
                                logger.info(f"✨ VOICEVOX 検出 (話者リスト判定): {base}")
                                self.engine_urls["voicevox"] = base
                                available.add("voicevox")
                                self.detected_ports.add(port)
                except Exception:
                    pass

        # Docker モード時: コンテナが存在するエンジンはスリープ中であってもサポート対象として認識
        if self.runtime == "docker":
            for eng, cname in DOCKER_CONTAINER_MAP.items():
                if eng not in available:
                    try:
                        subprocess.check_output(["docker", "inspect", cname], stderr=subprocess.DEVNULL)
                        available.add(eng)
                        logger.info(f"🐳 Docker エンジンコンテナ '{cname}' (休止中/待機) を提供対象として認識")
                    except Exception:
                        pass

        return list(available)

    async def collect_supported_models(self) -> Dict[str, List[str]]:
        """各エンジンが保有しているモデル・話者UUID一覧を収集"""
        models: Dict[str, List[str]] = {}
        if not self.session or self.session.closed:
            return models

        # 1. COEIROINK (v1/speakers から speakerUuid 一覧を取得)
        ci_url = self.engine_urls.get("coeiroink")
        if ci_url:
            try:
                async with self.session.get(f"{ci_url}/v1/speakers", timeout=aiohttp.ClientTimeout(total=4.0)) as r:
                    if r.status == 200:
                        speakers = await r.json()
                        uuids = []
                        for spk in speakers:
                            u = spk.get("speakerUuid")
                            if u:
                                uuids.append(str(u).lower())
                        models["coeiroink"] = uuids
                        logger.info(f"📦 [COEIROINK] 保有モデル検出: {len(uuids)} 話者 (例: {', '.join(uuids[:3])})")
            except Exception as e:
                logger.debug(f"COEIROINK models fetch error: {e}")

        # 2. VOICEVOX (speakers から speaker_uuid 一覧を取得)
        vv_url = self.engine_urls.get("voicevox")
        if vv_url:
            try:
                async with self.session.get(f"{vv_url}/speakers", timeout=aiohttp.ClientTimeout(total=4.0)) as r:
                    if r.status == 200:
                        speakers = await r.json()
                        uuids = [str(spk.get("speaker_uuid", "")).lower() for spk in speakers if spk.get("speaker_uuid")]
                        models["voicevox"] = uuids
                        logger.info(f"📦 [VOICEVOX] 保有話者検出: {len(uuids)} 話者")
            except Exception as e:
                logger.debug(f"VOICEVOX models fetch error: {e}")

        # 3. SHAREVOX (speakers から speaker_uuid 一覧を取得)
        sv_url = self.engine_urls.get("sharevox")
        if sv_url:
            try:
                async with self.session.get(f"{sv_url}/speakers", timeout=aiohttp.ClientTimeout(total=4.0)) as r:
                    if r.status == 200:
                        speakers = await r.json()
                        uuids = [str(spk.get("speaker_uuid", "")).lower() for spk in speakers if spk.get("speaker_uuid")]
                        models["sharevox"] = uuids
                        logger.info(f"📦 [SHAREVOX] 保有話者検出: {len(uuids)} 話者")
            except Exception as e:
                logger.debug(f"SHAREVOX models fetch error: {e}")

        return models

    def collect_specs(self) -> Dict[str, Any]:
        """PC スペック情報を収集"""
        specs = {
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "cpu": platform.processor() or "Generic CPU",
            "gpu": False,
            "gpu_name": "CPU"
        }
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                stderr=subprocess.DEVNULL
            ).decode().strip()
            if out:
                specs["gpu"] = True
                specs["gpu_name"] = out.split("\n")[0]
        except Exception:
            pass
        return specs

    # ==========================================
    # バックグラウンド監視ループ群
    # ==========================================
    async def _idle_monitor_loop(self):
        """Dockerモード時のアイドル自動スリープ (10分間リクエストなしで docker stop)"""
        if self.runtime != "docker":
            return
        logger.info(f"⏱️ [Docker] アイドル監視タスク開始 (無通信 {self.idle_timeout // 60}分でコンテナ自動スリープ)")
        while True:
            await asyncio.sleep(30.0)
            if self.active_tasks > 0:
                continue
            policy = self.idle_policy or {}
            if policy and not policy.get("enabled", True):
                continue
            idle_timeout = int(policy.get("timeout_sec") or self.idle_timeout)
            idle_sec = time.time() - self.last_activity_time
            if idle_sec >= idle_timeout:
                for eng in self.supported_engines:
                    cname = DOCKER_CONTAINER_MAP.get(eng)
                    if cname and self._is_container_running(cname):
                        logger.info(f"💤 [Idle Sleep] {idle_sec / 60:.1f}分間リクエストがなかったため、{eng} ({cname}) を一時停止してメモリ/CPUを解放します")
                        self._stop_docker_container(cname)

    async def _native_engine_monitor_loop(self):
        """ネイティブモード時: ユーザーがローカルエンジンを閉じた場合にエージェントも自動終了"""
        if self.runtime != "native" or not self.auto_exit:
            return
        # 起動直後の猶予期間（30秒）
        await asyncio.sleep(30.0)
        while True:
            await asyncio.sleep(15.0)
            if self.active_tasks > 0:
                continue
            if not self.detected_ports:
                continue

            any_alive = False
            for port in self.detected_ports:
                if await self._is_port_open("127.0.0.1", port):
                    any_alive = True
                    break

            if not any_alive:
                logger.info("🛑 [AutoExit] 提供中の音声合成エンジンがすべて終了したことを検出しました。")
                logger.info("   ユーザーがアプリを閉じたため、Volunteer Worker エージェントを自動終了します。")
                os._exit(0)

    # ==========================================
    # メインループ & WebSocket 接続
    # ==========================================
    async def run(self):
        """メインループ (常時接続 & 指数バックオフ再接続)"""
        logger.info("==================================================")
        logger.info(f"🚀 TTSBot Volunteer Worker Agent (v{PROTOCOL_VERSION})")
        logger.info(f"   Node ID   : {self.node_id}")
        logger.info(f"   Node Name : {self.node_name}")
        logger.info(f"   Runtime   : {self.runtime.upper()}")
        logger.info(f"   Server    : {self.server_url}")
        if self.runtime == "docker":
            logger.info(f"   Auto Sleep: {self.idle_timeout // 60}分アイドルで docker stop (省電力・低メモリ)")
        elif self.runtime == "native" and self.auto_exit:
            logger.info("   Auto Exit : ローカルエンジン終了時にエージェントも自動終了")
        logger.info("==================================================")

        # 1. ローカルエンジンの初回検出
        self.supported_engines = await self.detect_local_engines()
        if not self.supported_engines:
            logger.warning("⚠️ ローカルで起動している音声合成エンジン (VOICEVOX / COEIROINK / SHAREVOX) が見つかりませんでした。")
            if self.runtime != "docker":
                logger.warning("   エンジンを起動してから再実行してください。")

        # バックグラウンド監視タスク開始
        self._bg_tasks.append(asyncio.create_task(self._idle_monitor_loop()))
        self._bg_tasks.append(asyncio.create_task(self._native_engine_monitor_loop()))

        # 接続ループ
        backoff = 2.0
        try:
            while True:
                try:
                    await self._connect_and_serve()
                    backoff = 2.0
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"🔌 サーバー接続切断または失敗: {e}。 {backoff:.1f}秒後に再接続します...")
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 1.5)
                except Exception as e:
                    logger.error(f"❌ 予期しないエラー: {e}", exc_info=True)
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 1.5)
        finally:
            self.cleanup()

    def cleanup(self):
        """終了時クリーンアップ"""
        for t in self._bg_tasks:
            t.cancel()
        if self.runtime == "docker":
            logger.info("🧹 [Cleanup] Worker 終了に伴い、管理対象の Docker エンジンコンテナを安全に停止します...")
            for eng in self.supported_engines:
                cname = DOCKER_CONTAINER_MAP.get(eng)
                if cname and self._is_container_running(cname):
                    self._stop_docker_container(cname)

    async def _connect_and_serve(self):
        """1 回の WebSocket セッションの管理"""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        url = self.server_url
        if self.token and "?" not in url:
            url += f"?token={urllib.parse.quote(self.token)}"

        timeout = aiohttp.ClientTimeout(total=None, connect=10.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            async with session.ws_connect(
                url,
                headers=headers,
                heartbeat=20.0,
                max_msg_size=32 * 1024 * 1024
            ) as ws:
                logger.info(f"✅ サーバーに接続しました: {self.server_url}")

                self.supported_engines = await self.detect_local_engines()
                self.supported_models = await self.collect_supported_models()
                specs = self.collect_specs()

                reg_payload = {
                    "type": "register",
                    "node_id": self.node_id,
                    "node_name": self.node_name,
                    "supported_engines": self.supported_engines,
                    "supported_models": self.supported_models,
                    "capacity": self.capacity,
                    "priority": self.priority,
                    "exclusive_guild_id": self.exclusive_guild_id,
                    "specs": specs,
                    "device_id": self.device_id,
                    "client_version": PROTOCOL_VERSION
                }
                await ws.send_json(reg_payload)

                hb_task = asyncio.create_task(self._heartbeat_loop(ws))
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            import json
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue

                            m_type = data.get("type") or data.get("action")
                            if m_type == "register_ack":
                                # Master が端末 ID から確定した安定ノード ID を以後のハートビートで使う
                                if data.get("node_id"):
                                    self.node_id = data["node_id"]
                                self.idle_policy = data.get("idle_policy")
                                logger.info(f"🎉 サーバーにワーカーとして登録されました (Priority: {data.get('assigned_priority', self.priority)})")

                            elif m_type == "synthesize_request":
                                asyncio.create_task(self._handle_synthesize(ws, data))

                            elif m_type in ("update", "update_request"):
                                logger.info("🔄 [Update] サーバーからリモート自己アップデート指示を受信しました。最新版の取得を開始します...")
                                asyncio.create_task(self._handle_self_update())

                            elif m_type == "heartbeat_ack":
                                self.idle_policy = data.get("idle_policy")

                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.info("🔌 サーバーから切断通知を受信しました。")
                            break
                finally:
                    hb_task.cancel()

    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse):
        """10 秒間隔の生存報告ループ"""
        hb_count = 0
        while not ws.closed:
            try:
                await asyncio.sleep(10.0)
                hb_count += 1
                if hb_count % 6 == 0:
                    try:
                        self.supported_models = await self.collect_supported_models()
                    except Exception:
                        pass

                hb_data = {
                    "type": "heartbeat",
                    "node_id": self.node_id,
                    "active_tasks": self.active_tasks,
                    "cpu_percent": 0.0,
                    "memory_percent": 0.0,
                    "uptime_sec": round(time.time() - self.start_time, 1),
                    "supported_models": self.supported_models
                }
                await ws.send_json(hb_data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Heartbeat send error: {e}")
                break

    async def _handle_synthesize(self, ws: aiohttp.ClientWebSocketResponse, req: Dict[str, Any]):
        """サーバーからの音声合成リクエストをローカルエンジンへディスパッチして返送"""
        req_id = req.get("req_id", "")
        chunk_idx = req.get("chunk_index", 0)
        engine = req.get("engine", "voicevox").lower()
        speaker_id = req.get("speaker_id", 3)
        text = req.get("text", "")
        speed = float(req.get("speed", 1.0))
        pitch = float(req.get("pitch", 0.0))

        self.active_tasks += 1
        self.last_activity_time = time.time()
        t0 = time.time()
        preview = text[:20] + ("..." if len(text) > 20 else "")
        logger.info(f"🎙️ [Request] {engine.upper()} 合成開始: '{preview}' (req_id={req_id[:8]})")

        try:
            # Dockerモード時: スリープ中ならオンデマンドで復帰
            await self._ensure_engine_ready(engine)

            audio_bytes = await self._synthesize_on_engine(engine, speaker_id, text, speed, pitch)
            elapsed_ms = (time.time() - t0) * 1000.0

            bin_packet = pack_binary_chunk(req_id, chunk_idx, audio_bytes, is_final=True)
            await ws.send_bytes(bin_packet)
            logger.info(f"✨ [Success] {engine.upper()} 合成完了 ({elapsed_ms:.0f}ms, {len(audio_bytes)} bytes)")
        except Exception as e:
            logger.error(f"❌ [Failed] {engine.upper()} 合成失敗: {e}")
            try:
                await ws.send_json({
                    "type": "synthesize_error",
                    "req_id": req_id,
                    "chunk_index": chunk_idx,
                    "error_message": str(e)
                })
            except Exception:
                pass
        finally:
            self.active_tasks = max(0, self.active_tasks - 1)
            self.last_activity_time = time.time()

    async def _synthesize_on_engine(self, engine: str, speaker_id: Any, text: str, speed: float, pitch: float) -> bytes:
        """ローカルエンジンへの HTTP リクエスト実行"""
        if not self.session or self.session.closed:
            raise RuntimeError("HTTP session closed")

        target_base = self.engine_urls.get(engine)
        if not target_base:
            raise ValueError(f"Unsupported engine: {engine}")

        encoded_text = urllib.parse.quote(text)

        if engine in ("voicevox", "sharevox"):
            q_url = f"{target_base}/audio_query?text={encoded_text}&speaker={speaker_id}"
            async with self.session.post(q_url, timeout=aiohttp.ClientTimeout(total=5.0)) as q_resp:
                if q_resp.status != 200:
                    err = await q_resp.text()
                    raise RuntimeError(f"{engine} audio_query failed ({q_resp.status}): {err}")
                query_data = await q_resp.json()

            if speed != 1.0:
                query_data["speedScale"] = speed
            if pitch != 0.0:
                query_data["pitchScale"] = pitch

            s_url = f"{target_base}/synthesis?speaker={speaker_id}"
            async with self.session.post(s_url, json=query_data, timeout=aiohttp.ClientTimeout(total=8.0)) as s_resp:
                if s_resp.status != 200:
                    err = await s_resp.text()
                    raise RuntimeError(f"{engine} synthesis failed ({s_resp.status}): {err}")
                return await s_resp.read()

        elif engine == "coeiroink":
            style_id = int(str(speaker_id).split("_")[-1]) if "_" in str(speaker_id) else int(speaker_id)
            speaker_uuid = str(speaker_id).split("_")[0] if "_" in str(speaker_id) else "d312d0fb-d38d-434e-825d-cbcbfd105ad0"

            ep_url = f"{target_base}/v1/estimate_prosody"
            ep_payload = {"text": text}
            async with self.session.post(ep_url, json=ep_payload, timeout=aiohttp.ClientTimeout(total=5.0)) as ep_resp:
                if ep_resp.status != 200:
                    err = await ep_resp.text()
                    raise RuntimeError(f"coeiroink estimate_prosody failed ({ep_resp.status}): {err}")
                prosody_data = await ep_resp.json()

            s_url = f"{target_base}/v1/synthesis"
            synth_payload = {
                "speakerUuid": speaker_uuid,
                "styleId": style_id,
                "text": text,
                "prosodyDetail": prosody_data.get("detail", []),
                "speedScale": speed,
                "pitchScale": pitch,
                "volumeScale": 1.0,
                "intonationScale": 1.0,
                "prePhonemeLength": 0.1,
                "postPhonemeLength": 0.1,
                "outputSamplingRate": 24000
            }
            async with self.session.post(s_url, json=synth_payload, timeout=aiohttp.ClientTimeout(total=8.0)) as s_resp:
                if s_resp.status != 200:
                    err = await s_resp.text()
                    raise RuntimeError(f"coeiroink synthesis failed ({s_resp.status}): {err}")
                return await s_resp.read()

        raise ValueError(f"Unknown engine: {engine}")

    async def _handle_self_update(self):
        """Master から最新の volunteer_worker.py を取得して自プロセスを再起動 (os.execv)"""
        try:
            raw_url = self.server_url
            http_base = re.sub(r'^wss://', 'https://', raw_url)
            http_base = re.sub(r'^ws://', 'http://', http_base)
            http_base = re.sub(r'/api/worker/ws.*$', '', http_base).rstrip('/')
            update_url = f"{http_base}/volunteer/volunteer_worker.py"

            my_script = os.path.abspath(__file__)
            logger.info(f"📥 [Update] 最新コードをダウンロード中: {update_url}")

            timeout = aiohttp.ClientTimeout(total=15.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(update_url) as resp:
                    if resp.status == 200:
                        new_code = await resp.read()
                        if len(new_code) > 1000 and b"class VolunteerWorker" in new_code:
                            tmp_path = my_script + ".tmp"
                            with open(tmp_path, "wb") as f:
                                f.write(new_code)
                            os.replace(tmp_path, my_script)
                            logger.info("✨ [Update] 最新コードの保存完了！プロセスを即座に再起動します...")
                            await asyncio.sleep(0.5)
                            os.execv(sys.executable, [sys.executable] + sys.argv)
                        else:
                            logger.warning("⚠️ [Update] 取得したファイルが不正なサイズまたは内容のため、更新を中止しました。")
                    else:
                        logger.warning(f"⚠️ [Update] ダウンロード失敗 (HTTP {resp.status})")
        except Exception as e:
            logger.error(f"❌ [Update] 自己アップデート中にエラーが発生しました: {e}")

def main():
    parser = argparse.ArgumentParser(description="TTSBot Volunteer Worker Agent")
    parser.add_argument("--server-url", required=True, help="Master Server WebSocket URL (e.g. wss://tts.example.com/api/worker/ws)")
    parser.add_argument("--token", default="", help="Worker Authentication Shared Token")
    parser.add_argument("--name", default="", help="Friendly Name for this Worker Node")
    parser.add_argument("--capacity", type=int, default=2, help="Max concurrent tasks (default: 2)")
    parser.add_argument("--priority", type=int, default=50, help="Node priority (lower = higher priority, default: 50)")
    parser.add_argument("--exclusive-guild-id", default="", help="Dedicate this node to a specific Discord Guild ID (optional)")
    parser.add_argument("--game-priority", action="store_true", help="Run process with BelowNormal priority to protect game FPS")
    parser.add_argument("--runtime", choices=["native", "docker"], default="native", help="Execution runtime (native or docker)")
    parser.add_argument("--idle-timeout", type=int, default=600, help="Idle timeout in seconds before sleeping Docker engines (default: 600s)")
    parser.add_argument("--no-auto-exit", action="store_true", help="Disable auto-exit when native engines close")
    parser.add_argument("--voicevox-url", default="http://127.0.0.1:50021", help="Local VOICEVOX Engine URL")
    parser.add_argument("--coeiroink-url", default="http://127.0.0.1:50031", help="Local COEIROINK Engine URL")
    parser.add_argument("--sharevox-url", default="http://127.0.0.1:50025", help="Local SHAREVOX Engine URL")

    args = parser.parse_args()

    worker = VolunteerWorker(args)
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        logger.info("🛑 ユーザー操作によりワーカーを停止しました。")
    finally:
        worker.cleanup()

if __name__ == "__main__":
    main()
