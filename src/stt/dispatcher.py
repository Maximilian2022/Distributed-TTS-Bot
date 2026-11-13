import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set
import aiohttp
from aiohttp import web
import discord

logger = logging.getLogger(__name__)


class GameClientConnection:
    """ゲームPCクライアント (出口B) の WebSocket 接続"""

    def __init__(self, ws: web.WebSocketResponse, target_user_id: int):
        self.ws = ws
        self.target_user_id = target_user_id
        self.created_at = time.time()

    async def send_text(self, text: str, user_name: str, meta: Dict[str, Any]) -> bool:
        if self.ws.closed:
            return False
        try:
            await self.ws.send_json({
                "type": "game_chat",
                "text": text,
                "user_name": user_name,
                "user_id": self.target_user_id,
                "timestamp": time.time(),
                "seq": meta.get("seq", 0),
            })
            return True
        except Exception as e:
            logger.debug(f"Game client send error: {e}")
            return False


class SpeechDispatcher:
    """
    早耳 Live の STT 結果を Discord テキストチャンネル (出口A) と
    ゲームPCクライアント (出口B) へ安全に分配するディスパッチャ
    """

    def __init__(
        self,
        bot: discord.Client,
        text_channel_id: Optional[int] = None,
        buffer_seconds: float = 1.0,
    ):
        self.bot = bot
        self.text_channel_id = text_channel_id
        self.buffer_seconds = max(0.3, min(5.0, buffer_seconds))

        # 出口A: Discord 投稿用バッファ
        self._buffer: List[Dict[str, Any]] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._last_send_time = 0.0
        self._min_send_interval = 1.0  # Discord API レートリミット保護 (最大1秒に1回)

        # 出口B: ゲームPC接続リスト
        self._game_clients: Set[GameClientConnection] = set()
        self._game_clients_lock = asyncio.Lock()

        # 発話 seq と直近発話エントリのマッピング (翻訳テキストのマージ用)
        self._seq_map: Dict[str, Dict[str, Any]] = {}

    def set_text_channel(self, channel_id: Optional[int]) -> None:
        """投稿先テキストチャンネルを変更"""
        self.text_channel_id = channel_id

    async def register_game_client(self, ws: web.WebSocketResponse, target_user_id: int) -> GameClientConnection:
        """ゲームPC WebSocket クライアントを登録"""
        conn = GameClientConnection(ws, target_user_id)
        async with self._game_clients_lock:
            self._game_clients.add(conn)
        logger.info(f"🎮 [GameBridge] Registered client for target user {target_user_id}")
        return conn

    async def unregister_game_client(self, conn: GameClientConnection) -> None:
        """ゲームPC WebSocket クライアントを解除"""
        async with self._game_clients_lock:
            self._game_clients.discard(conn)
        logger.info(f"🎮 [GameBridge] Unregistered client for target user {conn.target_user_id}")

    async def on_transcript(self, user_id: int, user_name: str, data: Dict[str, Any]) -> None:
        """確定字幕イベントを受信"""
        text = str(data.get("text", "")).strip()
        if not text:
            return

        is_final = data.get("is_final", True)
        if not is_final:
            return

        seq = data.get("seq", 0)
        seq_key = f"{user_id}_{seq}"
        logger.info(f"✨ [STTDispatcher] Transcript #{seq} for {user_name} ({user_id}): {text}")

        # 1. 【出口B】 ゲームPCへ即時プッシュ (Discordのバッファリング待機なし、0ms)
        async with self._game_clients_lock:
            clients_to_send = [c for c in self._game_clients if c.target_user_id in (user_id, 0)]
        for client in clients_to_send:
            await client.send_text(text, user_name, data)

        # 2. 【出口A】 Discord テキストチャンネル用のバッファリング
        if not self.text_channel_id:
            return

        entry = {
            "user_id": user_id,
            "user_name": user_name,
            "text": text,
            "trans": "",
            "seq_key": seq_key,
            "ts": time.time(),
        }
        self._seq_map[seq_key] = entry

        async with self._buffer_lock:
            self._buffer.append(entry)
            if not self._flush_task or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._scheduled_flush())

    async def on_translation(self, user_id: int, user_name: str, data: Dict[str, Any]) -> None:
        """翻訳イベントを受信"""
        trans_text = str(data.get("text", "")).strip()
        if not trans_text:
            return

        seq = data.get("seq", 0)
        seq_key = f"{user_id}_{seq}"

        # 既存のバッファエントリがあればインライン統合
        if seq_key in self._seq_map:
            entry = self._seq_map[seq_key]
            entry["trans"] = trans_text
        else:
            # 万が一字幕より先に翻訳が届いた場合のフォールバック
            entry = {
                "user_id": user_id,
                "user_name": user_name,
                "text": str(data.get("original", "")).strip() or "...",
                "trans": trans_text,
                "seq_key": seq_key,
                "ts": time.time(),
            }
            self._seq_map[seq_key] = entry
            async with self._buffer_lock:
                self._buffer.append(entry)
                if not self._flush_task or self._flush_task.done():
                    self._flush_task = asyncio.create_task(self._scheduled_flush())

    async def _scheduled_flush(self) -> None:
        """バッファ秒数待機し、まとめて Discord へ送信"""
        await asyncio.sleep(self.buffer_seconds)

        async with self._buffer_lock:
            if not self._buffer:
                return
            entries = list(self._buffer)
            self._buffer.clear()

        # レートリミット (最大 1秒に1回) のインターバル制御
        now = time.time()
        elapsed = now - self._last_send_time
        if elapsed < self._min_send_interval:
            await asyncio.sleep(self._min_send_interval - elapsed)

        await self._send_to_discord(entries)
        self._last_send_time = time.time()

        # seq_map のクリーンアップ (過去50件超を破棄)
        if len(self._seq_map) > 100:
            keys_to_remove = list(self._seq_map.keys())[:-50]
            for k in keys_to_remove:
                self._seq_map.pop(k, None)

    async def _send_to_discord(self, entries: List[Dict[str, Any]]) -> None:
        """まとめた発話を 1 つの Discord メッセージとして投稿"""
        if not self.text_channel_id:
            return

        channel = self.bot.get_channel(self.text_channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(self.text_channel_id)
            except Exception:
                return

        lines = []
        for e in entries:
            name = e["user_name"]
            txt = e["text"]
            trans = f" ({e['trans']})" if e["trans"] else ""
            lines.append(f"🎙️ **{name}**: {txt}{trans}")

        if not lines:
            return

        message_content = "\n".join(lines)
        if len(message_content) > 1950:
            message_content = message_content[:1950] + "..."

        try:
            await channel.send(message_content)
            logger.info(f"📤 [STTDispatcher] Posted to text channel {self.text_channel_id} ({len(entries)} speech items)")
        except discord.HTTPException as e:
            if e.status == 429:
                logger.warning(f"⚠️ [STT Dispatcher] Rate limited (429) on channel {self.text_channel_id}")
            else:
                logger.error(f"❌ [STT Dispatcher] Failed to send message: {e}")
        except Exception as e:
            logger.error(f"❌ [STT Dispatcher] Unexpected send error: {e}")

