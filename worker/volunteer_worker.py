#!/usr/bin/env python3
"""
TTSBot Volunteer Worker Agent
ボランティア型分散音声合成ワーカークライアント

【概要】
- ポート開放不要: クライアントPCからサーバー (WSS) へアウトバウンド接続
- 自動エンジン検出: ローカルで起動している VOICEVOX / COEIROINK / SHAREVOX を自動検出
- ゼロコピーバイナリ転送: 高速なバイナリWebSocketフレームで音声を返送
- 自動再接続: 切断時の指数バックオフリトライ

【実行例】
python volunteer_worker.py --server-url wss://tts.example.com/api/worker/ws --token YOUR_TOKEN --name "MyGamingPC"
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
import urllib.parse
from typing import Dict, Any, List, Optional

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
PROTOCOL_VERSION = "2.0.0"
BINARY_HEADER_FORMAT = ">32sIB"
FLAG_FINAL_CHUNK = 0x01

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
        self.capacity = args.capacity
        self.priority = args.priority
        
        self.engine_urls = {
            "voicevox": args.voicevox_url.rstrip("/"),
            "coeiroink": args.coeiroink_url.rstrip("/"),
            "sharevox": args.sharevox_url.rstrip("/"),
        }
        self.supported_engines: List[str] = []
        self.session: Optional[aiohttp.ClientSession] = None
        self.active_tasks = 0
        self.start_time = time.time()

    async def detect_local_engines(self) -> List[str]:
        """ローカルで稼働している各音声合成エンジンの死活を判定"""
        available = []
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            # VOICEVOX (50021)
            try:
                async with s.get(f"{self.engine_urls['voicevox']}/version") as r:
                    if r.status == 200:
                        ver = await r.text()
                        logger.info(f"✨ VOICEVOX 検出: v{ver.strip()} ({self.engine_urls['voicevox']})")
                        available.append("voicevox")
            except Exception:
                pass

            # COEIROINK (50031)
            try:
                async with s.get(f"{self.engine_urls['coeiroink']}/v1/speakers") as r:
                    if r.status == 200:
                        logger.info(f"✨ COEIROINK 検出: ({self.engine_urls['coeiroink']})")
                        available.append("coeiroink")
            except Exception:
                pass

            # SHAREVOX (50025)
            try:
                async with s.get(f"{self.engine_urls['sharevox']}/version") as r:
                    if r.status == 200:
                        ver = await r.text()
                        logger.info(f"✨ SHAREVOX 検出: v{ver.strip()} ({self.engine_urls['sharevox']})")
                        available.append("sharevox")
            except Exception:
                pass

        return available

    def collect_specs(self) -> Dict[str, Any]:
        """PC スペック情報を収集"""
        specs = {
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "cpu": platform.processor() or "Generic CPU",
            "gpu": False,
            "gpu_name": "CPU"
        }
        # NVIDIA GPU 検出試行 (Windows / Linux)
        try:
            import subprocess
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

    async def run(self):
        """メインループ (常時接続 & 指数バックオフ再接続)"""
        logger.info("==================================================")
        logger.info(f"🚀 TTSBot Volunteer Worker Agent (v{PROTOCOL_VERSION})")
        logger.info(f"   Node ID   : {self.node_id}")
        logger.info(f"   Node Name : {self.node_name}")
        logger.info(f"   Server    : {self.server_url}")
        logger.info("==================================================")

        # 1. ローカルエンジンの初回検出
        self.supported_engines = await self.detect_local_engines()
        if not self.supported_engines:
            logger.warning("⚠️ ローカルで起動している音声合成エンジン (VOICEVOX / COEIROINK / SHAREVOX) が見つかりませんでした。")
            logger.warning("   エンジンを起動してから再実行してください。")
            # 終了せず、後から起動される可能性も考慮して継続

        # 接続ループ
        backoff = 2.0
        while True:
            try:
                await self._connect_and_serve()
                backoff = 2.0  # 正常終了時はリセット
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"🔌 サーバー接続切断または失敗: {e}。 {backoff:.1f}秒後に再接続します...")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.5)
            except Exception as e:
                logger.error(f"❌ 予期しないエラー: {e}", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.5)

    async def _connect_and_serve(self):
        """1 回の WebSocket セッションの管理"""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        # URLパラメータにトークンも含める（Cloudflare / リバースプロキシ対策）
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

                # 最新のエンジン状態とスペックを再収集
                self.supported_engines = await self.detect_local_engines()
                specs = self.collect_specs()

                # 登録メッセージ送信
                reg_payload = {
                    "type": "register",
                    "node_id": self.node_id,
                    "node_name": self.node_name,
                    "supported_engines": self.supported_engines,
                    "capacity": self.capacity,
                    "priority": self.priority,
                    "specs": specs,
                    "client_version": PROTOCOL_VERSION
                }
                await ws.send_json(reg_payload)

                # ハートビート送信タスク起動
                hb_task = asyncio.create_task(self._heartbeat_loop(ws))

                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            import json
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue

                            m_type = data.get("type")
                            if m_type == "register_ack":
                                logger.info(f"🎉 サーバーにワーカーとして登録されました (Priority: {data.get('assigned_priority', self.priority)})")

                            elif m_type == "synthesize_request":
                                # バックグラウンドタスクとして音声合成を実行 (並列実行)
                                asyncio.create_task(self._handle_synthesize(ws, data))

                            elif m_type == "heartbeat_ack":
                                pass

                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.info("🔌 サーバーから切断通知を受信しました。")
                            break
                finally:
                    hb_task.cancel()

    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse):
        """10 秒間隔の生存報告ループ"""
        while not ws.closed:
            try:
                await asyncio.sleep(10.0)
                hb_data = {
                    "type": "heartbeat",
                    "node_id": self.node_id,
                    "active_tasks": self.active_tasks,
                    "cpu_percent": 0.0,
                    "memory_percent": 0.0,
                    "uptime_sec": round(time.time() - self.start_time, 1)
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
        t0 = time.time()
        preview = text[:20] + ("..." if len(text) > 20 else "")
        logger.info(f"🎙️ [Request] {engine.upper()} 合成開始: '{preview}' (req_id={req_id[:8]})")

        try:
            audio_bytes = await self._synthesize_on_engine(engine, speaker_id, text, speed, pitch)
            elapsed_ms = (time.time() - t0) * 1000.0

            # ゼロコピー・バイナリフレームをパッキングして即時返信
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

    async def _synthesize_on_engine(self, engine: str, speaker_id: Any, text: str, speed: float, pitch: float) -> bytes:
        """ローカルエンジンへの HTTP リクエスト実行"""
        if not self.session or self.session.closed:
            raise RuntimeError("HTTP session closed")

        target_base = self.engine_urls.get(engine)
        if not target_base:
            raise ValueError(f"Unsupported engine: {engine}")

        encoded_text = urllib.parse.quote(text)

        if engine in ("voicevox", "sharevox"):
            # 1. audio_query
            q_url = f"{target_base}/audio_query?text={encoded_text}&speaker={speaker_id}"
            async with self.session.post(q_url, timeout=aiohttp.ClientTimeout(total=5.0)) as q_resp:
                if q_resp.status != 200:
                    err = await q_resp.text()
                    raise RuntimeError(f"{engine} audio_query failed ({q_resp.status}): {err}")
                query_data = await q_resp.json()

            # パラメータ調整
            if speed != 1.0:
                query_data["speedScale"] = speed
            if pitch != 0.0:
                query_data["pitchScale"] = pitch

            # 2. synthesis
            s_url = f"{target_base}/synthesis?speaker={speaker_id}"
            async with self.session.post(s_url, json=query_data, timeout=aiohttp.ClientTimeout(total=8.0)) as s_resp:
                if s_resp.status != 200:
                    err = await s_resp.text()
                    raise RuntimeError(f"{engine} synthesis failed ({s_resp.status}): {err}")
                return await s_resp.read()

        elif engine == "coeiroink":
            # COEIROINK v1
            style_id = int(str(speaker_id).split("_")[-1]) if "_" in str(speaker_id) else int(speaker_id)
            speaker_uuid = str(speaker_id).split("_")[0] if "_" in str(speaker_id) else "d312d0fb-d38d-434e-825d-cbcbfd105ad0"

            # 1. estimate_prosody
            ep_url = f"{target_base}/v1/estimate_prosody"
            ep_payload = {"text": text}
            async with self.session.post(ep_url, json=ep_payload, timeout=aiohttp.ClientTimeout(total=5.0)) as ep_resp:
                if ep_resp.status != 200:
                    err = await ep_resp.text()
                    raise RuntimeError(f"coeiroink estimate_prosody failed ({ep_resp.status}): {err}")
                prosody_data = await ep_resp.json()

            # 2. synthesis
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

def main():
    parser = argparse.ArgumentParser(description="TTSBot Volunteer Worker Agent")
    parser.add_argument("--server-url", required=True, help="Master Server WebSocket URL (e.g. wss://tts.example.com/api/worker/ws)")
    parser.add_argument("--token", default="", help="Worker Authentication Shared Token")
    parser.add_argument("--name", default="", help="Friendly Name for this Worker Node")
    parser.add_argument("--capacity", type=int, default=2, help="Max concurrent tasks (default: 2)")
    parser.add_argument("--priority", type=int, default=50, help="Node priority (lower = higher priority, default: 50)")
    parser.add_argument("--voicevox-url", default="http://127.0.0.1:50021", help="Local VOICEVOX Engine URL")
    parser.add_argument("--coeiroink-url", default="http://127.0.0.1:50031", help="Local COEIROINK Engine URL")
    parser.add_argument("--sharevox-url", default="http://127.0.0.1:50025", help="Local SHAREVOX Engine URL")

    args = parser.parse_args()

    worker = VolunteerWorker(args)
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        logger.info("🛑 ユーザー操作によりワーカーを停止しました。")

if __name__ == "__main__":
    main()
