import asyncio
import json
import logging
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional
import aiohttp

logger = logging.getLogger(__name__)


class HayamimiSession:
    """早耳 Live (Hayamimi) との 1 話者分の WebSocket ストリーミングセッション"""

    def __init__(
        self,
        user_id: int,
        user_name: str,
        gateway_url: str,
        model: str = "reazon",
        trans_langs: str = "",
        gemini_key: str = "",
        token: str = "",
        on_transcript: Optional[Callable[[int, str, Dict[str, Any]], Any]] = None,
        on_translation: Optional[Callable[[int, str, Dict[str, Any]], Any]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self.user_id = user_id
        self.user_name = user_name
        self.gateway_url = gateway_url
        self.model = model or "reazon"
        self.trans_langs = trans_langs or ""
        self.gemini_key = gemini_key or ""
        self.token = token or ""
        self.on_transcript = on_transcript
        self.on_translation = on_translation
        self._loop = loop

        self.session_id: Optional[str] = None
        self.is_connected = False
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=150)
        self._recv_task: Optional[asyncio.Task] = None
        self._send_task: Optional[asyncio.Task] = None
        self._closed = False

    def build_ws_url(self) -> str:
        """接続用 WebSocket URL を生成"""
        params = {
            "sample_rate": 48000,
            "channels": 2,
            "format": "s16le",
            "model": self.model,
        }
        if self.trans_langs:
            params["trans_langs"] = self.trans_langs
        if self.gemini_key:
            params["gemini_key"] = self.gemini_key
        if self.token:
            params["token"] = self.token

        query_str = urllib.parse.urlencode(params)
        delimiter = "&" if "?" in self.gateway_url else "?"
        return f"{self.gateway_url}{delimiter}{query_str}"

    async def start(self) -> bool:
        """WebSocket 接続を開始し、ready 受信後に送受信タスクを立ち上げる"""
        if self._closed:
            return False

        if not self._loop:
            self._loop = asyncio.get_running_loop()

        target_url = self.build_ws_url()
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=5.0)
            )
            self._ws = await self._session.ws_connect(target_url, heartbeat=15.0)

            # サーバーからの ready メッセージを待機 (初期化完了を確実に保証)
            ready_msg = await self._ws.receive()
            if ready_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(ready_msg.data)
                    if data.get("type") == "ready":
                        self.session_id = data.get("session_id")
                        logger.info(f"✨ [Hayamimi] Session ready: {self.session_id} (user: {self.user_name})")
                except Exception:
                    pass

            self.is_connected = True

            self._recv_task = asyncio.create_task(self._recv_loop(), name=f"hayamimi_recv_{self.user_id}")
            self._send_task = asyncio.create_task(self._send_loop(), name=f"hayamimi_send_{self.user_id}")
            logger.info(f"🎙️ [Hayamimi] Connected session for user {self.user_name} ({self.user_id})")
            return True
        except Exception as e:
            logger.error(f"❌ [Hayamimi] Failed to connect for user {self.user_id}: {e}")
            await self.close()
            return False

    def push_audio(self, pcm_chunk: bytes) -> None:
        """48kHz Stereo 16-bit PCM (20ms = 3840 bytes) を送信キューに投入 (スレッドセーフ)"""
        if self._closed or not self.is_connected or not self._loop:
            return

        def _do_put():
            try:
                self._send_queue.put_nowait(pcm_chunk)
            except asyncio.QueueFull:
                try:
                    self._send_queue.get_nowait()
                    self._send_queue.put_nowait(pcm_chunk)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        try:
            self._loop.call_soon_threadsafe(_do_put)
        except RuntimeError:
            pass

    async def _send_loop(self) -> None:
        """PCM バイナリフレームを順次送信"""
        sent_bytes = 0
        sent_chunks = 0
        last_log_time = time.time()
        try:
            while not self._closed and self._ws and not self._ws.closed:
                chunk = await self._send_queue.get()
                if self._ws and not self._ws.closed:
                    await self._ws.send_bytes(chunk)
                    sent_bytes += len(chunk)
                    sent_chunks += 1
                    now = time.time()
                    if now - last_log_time >= 5.0 and sent_chunks > 0:
                        last_log_time = now
                        logger.debug(
                            f"📡 [Hayamimi Send] {self.user_name} ({self.user_id}): "
                            f"{sent_chunks} chunks ({sent_bytes / 1024:.1f} KB) sent to session {self.session_id}"
                        )
                self._send_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._closed:
                logger.warning(f"⚠️ [Hayamimi] Send error for user {self.user_id}: {e}")

    async def _recv_loop(self) -> None:
        """早耳 Live からの JSON イベント (ready, transcript, translation, error) を受信"""
        try:
            while not self._closed and self._ws and not self._ws.closed:
                msg = await self._ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ [Hayamimi WS] Non-JSON text received: {msg.data[:100]}")
                        continue

                    msg_type = data.get("type")
                    if msg_type == "ready":
                        self.session_id = data.get("session_id")
                        logger.info(f"✨ [Hayamimi] Session ready: {self.session_id} (user: {self.user_name})")
                    elif msg_type == "transcript":
                        text = data.get("text", "")
                        is_final = data.get("is_final", False)
                        seq = data.get("seq")
                        lat = data.get("latency_ms")
                        logger.info(
                            f"✨ [Hayamimi WS] Transcript: user='{self.user_name}' (ID: {self.user_id}) | "
                            f"final={is_final} | seq={seq} | latency={lat}ms | text='{text}'"
                        )
                        if self.on_transcript:
                            try:
                                res = self.on_transcript(self.user_id, self.user_name, data)
                                if asyncio.iscoroutine(res):
                                    asyncio.create_task(res)
                            except Exception as ex:
                                logger.exception(f"Error in on_transcript callback: {ex}")
                    elif msg_type == "translation":
                        lang = data.get("lang")
                        trans_text = data.get("text")
                        logger.info(
                            f"🌐 [Hayamimi WS] Translation: user='{self.user_name}' | "
                            f"lang={lang} | text='{trans_text}'"
                        )
                        if self.on_translation:
                            try:
                                res = self.on_translation(self.user_id, self.user_name, data)
                                if asyncio.iscoroutine(res):
                                    asyncio.create_task(res)
                            except Exception as ex:
                                logger.exception(f"Error in on_translation callback: {ex}")
                    elif msg_type == "error":
                        logger.warning(f"⚠️ [Hayamimi WS] Server error: {data.get('message')} (user: {self.user_name})")
                    elif msg_type == "ping":
                        await self._ws.send_str(json.dumps({"type": "pong", "ts": time.time()}))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._closed:
                logger.warning(f"⚠️ [Hayamimi] Receive error for user {self.user_id}: {e}")
        finally:
            if not self._closed:
                await self.close()

    async def close(self) -> None:
        """セッションの正常終了"""
        self._closed = True
        self.is_connected = False
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.debug(f"🛑 [Hayamimi] Session closed for user {self.user_id}")

