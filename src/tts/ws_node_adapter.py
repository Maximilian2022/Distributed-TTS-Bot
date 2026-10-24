"""
ボランティア型分散ワーカー用 WebSocket ノードアダプタ (WSWorkerNode)
- TTSNode を継承し、TTSNodePool と完全互換
- 常時接続 WebSocket (WSS) 経由での音声合成リクエスト/レスポンス制御
- ゼロコピー・バイナリフレームによる超低遅延音声ストリーミング
"""

import asyncio
import time
import logging
import uuid
from typing import Dict, Any, Optional, List, Union
from aiohttp import web

from src.config import TTSNodeConfig
from src.tts.base import TTSError, TTSConnectionError
from src.tts.node_pool import TTSNode
from src.tts.protocols.worker_protocol import (
    SynthesizeRequest,
    RegisterAckMessage,
    HeartbeatMessage,
    HeartbeatAckMessage,
    PROTOCOL_VERSION
)

logger = logging.getLogger(__name__)

class WSWorkerNode(TTSNode):
    """
    WebSocket 双方向通信で接続されたボランティア Worker ノード
    ポート開放不要 (NAT/ルーター内からのアウトバウンド接続) でプールに参加
    """

    def __init__(
        self,
        node_id: str,
        node_name: str,
        ws: web.WebSocketResponse,
        supported_engines: List[str],
        capacity: int = 2,
        specs: Optional[Dict[str, Any]] = None,
        priority: int = 50
    ):
        config = TTSNodeConfig(
            id=node_id,
            url=f"ws://{node_id}",  # 仮想URL
            priority=priority,
            max_concurrent=max(1, capacity),
            enabled=True
        )
        super().__init__(config, specs=specs, hostname=node_name)
        self.is_ws: bool = True
        self.ws: web.WebSocketResponse = ws
        self.supported_engines: List[str] = [e.lower() for e in supported_engines]
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.last_heartbeat = time.time()
        self.is_healthy = True
        self.engine_running = True

    @property
    def machine_name(self) -> str:
        """UI/ログ用マシン表示"""
        gpu = self.specs.get("gpu_name") or ("GPU" if self.specs.get("gpu") else "CPU")
        gpu_clean = str(gpu).replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
        return f"Volunteer-{self.hostname} ({gpu_clean})"

    @property
    def short_label(self) -> str:
        engines_str = ",".join(self.supported_engines).upper()
        return f"{self.machine_name} [WS/{engines_str}]"

    @property
    def summary_label(self) -> str:
        return f"{self.machine_name} [WS]"

    @property
    def is_external(self) -> bool:
        return True

    @property
    def is_available(self) -> bool:
        """WebSocket 接続中かつ生存しているか"""
        if self.ws.closed:
            return False
        if not self.enabled:
            return False
        if not self.is_healthy:
            return False
        if time.time() < self.unhealthy_until:
            return False
        # 直近45秒以内にハートビート受信があるか
        if time.time() - self.last_heartbeat > 45.0:
            return False
        return True

    def update_from_heartbeat(self, msg: HeartbeatMessage):
        """ハートビート情報からスペック・負荷・死活を更新"""
        self.last_heartbeat = time.time()
        self.is_healthy = True
        self.unhealthy_until = 0.0
        self.specs["cpu_percent"] = msg.cpu_percent
        self.specs["memory_percent"] = msg.memory_percent
        self.specs["active_tasks"] = msg.active_tasks
        self.specs["uptime_sec"] = msg.uptime_sec
        if msg.engine_status:
            self.specs["engine_status"] = msg.engine_status

    def to_dict(self) -> Dict[str, Any]:
        """WebUIステータス用辞書"""
        d = super().to_dict()
        d["is_volunteer"] = True
        d["connection_type"] = "websocket"
        d["supported_engines"] = self.supported_engines
        return d

    async def synthesize(
        self,
        engine: str,
        text: str,
        voice_name: str,
        pitch: str = "0%",
        speed: float = 1.0,
        timeout_seconds: float = 8.0,
        chunk_index: int = 0
    ) -> bytes:
        """
        WebSocket を通じて Worker へ音声合成リクエストを送信し、
        バイナリ音声データを受信して返却する
        """
        if self.ws.closed:
            raise TTSConnectionError(f"Worker {self.id} WebSocket connection is closed")

        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        self.pending_requests[req_id] = fut

        # pitch 文字列 ("+10%", "0%") を float に変換
        pitch_val = 0.0
        try:
            pitch_clean = str(pitch).replace("%", "").replace("+", "").strip()
            pitch_val = float(pitch_clean) / 100.0
        except Exception:
            pitch_val = 0.0

        req = SynthesizeRequest(
            req_id=req_id,
            chunk_index=chunk_index,
            engine=engine.lower(),
            speaker_id=voice_name,
            text=text,
            speed=speed,
            pitch=pitch_val,
            timeout_ms=int(timeout_seconds * 1000)
        )

        t0 = time.time()
        self.total_requests += 1

        try:
            from dataclasses import asdict
            await self.ws.send_json(asdict(req))
            audio_data = await asyncio.wait_for(fut, timeout=timeout_seconds)
            latency_ms = (time.time() - t0) * 1000.0
            self.mark_success(latency_ms)
            return audio_data
        except asyncio.TimeoutError:
            self.mark_failed(cooldown_seconds=20.0, reason=f"WS synthesis timeout ({timeout_seconds}s)")
            # Worker 側にキャンセル通知を試行 (fire-and-forget)
            try:
                if not self.ws.closed:
                    await self.ws.send_json({"type": "synthesize_cancel", "req_id": req_id})
            except Exception:
                pass
            raise TTSConnectionError(f"Worker {self.id} timed out synthesizing text ({timeout_seconds}s)")
        except Exception as e:
            self.mark_failed(cooldown_seconds=15.0, reason=str(e))
            raise
        finally:
            self.pending_requests.pop(req_id, None)

    def on_binary_chunk(self, req_id: str, chunk_index: int, is_final: bool, payload: bytes):
        """ワーカーからバイナリ音声フレームを受信した際のディスパッチ"""
        fut = self.pending_requests.get(req_id)
        if fut and not fut.done():
            # 現状は一括または最終チャンクで完了
            fut.set_result(payload)

    def on_error(self, req_id: str, error_message: str):
        """ワーカーからのエラー通知ディスパッチ"""
        fut = self.pending_requests.get(req_id)
        if fut and not fut.done():
            fut.set_exception(TTSError(f"Worker {self.id} error: {error_message}"))

    async def close(self):
        """ノード解放・接続切断"""
        # 未処理のリクエストを全てキャンセル
        for req_id, fut in list(self.pending_requests.items()):
            if not fut.done():
                fut.set_exception(TTSConnectionError("Node closed"))
        self.pending_requests.clear()
        if not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass
        await super().close()
