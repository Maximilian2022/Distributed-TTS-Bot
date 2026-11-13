import asyncio
import logging
from typing import Dict, Optional, Set
import discord
from aiohttp import web

from src.config import AppConfig
from src.database import Database
from src.stt.dispatcher import SpeechDispatcher, GameClientConnection
import src.stt.voice_receiver

logger = logging.getLogger(__name__)


class STTManager:
    """早耳 Live STT & リアルタイム翻訳ストリーミングの統合統括マネージャー"""

    _instance: Optional["STTManager"] = None

    def __init__(self, bot: discord.Client, db: Database, config: AppConfig):
        self.bot = bot
        self.db = db
        self.config = config

        # guild_id -> VoiceStreamReceiver
        self.receivers: Dict[int, src.stt.voice_receiver.VoiceStreamReceiver] = {}
        # guild_id -> SpeechDispatcher
        self.dispatchers: Dict[int, SpeechDispatcher] = {}

        # グローバルなゲームPCクライアント (出口B) 管理ディスパッチャ
        self.global_dispatcher = SpeechDispatcher(bot=bot, text_channel_id=None, buffer_seconds=1.0)
        STTManager._instance = self

    @classmethod
    def get_instance(cls) -> Optional["STTManager"]:
        return cls._instance

    def is_active(self, guild_id: int) -> bool:
        return guild_id in self.receivers and self.receivers[guild_id]._is_running

    async def start_stt(
        self,
        guild: discord.Guild,
        text_channel_id: Optional[int] = None,
        target_user_ids: Optional[Set[int]] = None,
        trans_langs: Optional[str] = None,
        model: Optional[str] = None,
        voice_client: Optional[discord.VoiceClient] = None,
        bot_client: Optional[discord.Client] = None,
    ) -> bool:
        """指定ギルドで STT 音声ストリーミングを開始"""
        vc: Optional[discord.VoiceClient] = voice_client or guild.voice_client
        if not vc or not vc.is_connected():
            logger.warning(f"Cannot start STT: Bot is not connected to VC in guild {guild.id}")
            return False

        # 既存セッションがあれば停止
        await self.stop_stt(guild.id)

        gateway_url = self.config.hayamimi_ws_url
        if not gateway_url:
            logger.error("HAYAMIMI_WS_URL is not configured.")
            return False

        asr_model = model or getattr(self.config, "hayamimi_model", "reazon")
        langs = trans_langs if trans_langs is not None else getattr(self.config, "hayamimi_trans_langs", "")
        gemini_key = getattr(self.config, "hayamimi_gemini_key", "")
        token = getattr(self.config, "hayamimi_token", "")
        buf_sec = getattr(self.config, "hayamimi_buffer_seconds", 1.0)
        max_speakers = getattr(self.config, "hayamimi_max_speakers", 8)

        # ディスパッチャの生成 (出口A: Discord テキストチャンネル)
        bot_to_use = bot_client or getattr(vc, "client", None) or self.bot
        dispatcher = SpeechDispatcher(
            bot=bot_to_use,
            text_channel_id=text_channel_id,
            buffer_seconds=buf_sec,
        )
        self.dispatchers[guild.id] = dispatcher

        # レシーバーの生成 (モジュールから動的参照してリロード時も常に最新クラスを使用)
        receiver = src.stt.voice_receiver.VoiceStreamReceiver(
            voice_client=vc,
            dispatcher=dispatcher,
            gateway_url=gateway_url,
            model=asr_model,
            trans_langs=langs,
            gemini_key=gemini_key,
            token=token,
            target_user_ids=target_user_ids,
            max_speakers=max_speakers,
        )

        success = await receiver.start()
        if success:
            self.receivers[guild.id] = receiver
            logger.info(f"🎙️ [STTManager] Successfully started STT for guild {guild.name} ({guild.id})")
            return True
        else:
            await dispatcher._scheduled_flush()
            self.dispatchers.pop(guild.id, None)
            return False

    async def stop_stt(self, guild_id: int) -> None:
        """指定ギルドの STT ストリーミングを停止"""
        receiver = self.receivers.pop(guild_id, None)
        if receiver:
            await receiver.stop()

        dispatcher = self.dispatchers.pop(guild_id, None)
        if dispatcher:
            await dispatcher._scheduled_flush()

        logger.info(f"🛑 [STTManager] Stopped STT for guild {guild_id}")

    async def handle_game_ws(self, request: web.Request) -> web.WebSocketResponse:
        """ゲームPCクライアント向け WebSocket ハンドラ (/api/stt/stream)"""
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)

        user_id_raw = request.query.get("user_id", "0")
        try:
            target_user_id = int(user_id_raw)
        except ValueError:
            target_user_id = 0

        logger.info(f"🎮 [GameBridge] New connection from {request.remote} for user_id={target_user_id}")
        await ws.send_json({
            "type": "hello",
            "message": "Connected to TTSBot Hayamimi Game Bridge",
            "target_user_id": target_user_id,
        })

        # 全ディスパッチャおよびグローバルディスパッチャにクライアントを登録
        client_conns = []
        for disp in list(self.dispatchers.values()) + [self.global_dispatcher]:
            conn = await disp.register_game_client(ws, target_user_id)
            client_conns.append((disp, conn))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # ping や設定変更のハンドリング
                    try:
                        data = msg.json()
                        if data.get("type") == "ping":
                            await ws.send_json({"type": "pong"})
                    except Exception:
                        pass
                elif msg.type in (web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                    break
        finally:
            for disp, conn in client_conns:
                await disp.unregister_game_client(conn)
            logger.info(f"🎮 [GameBridge] Connection closed for {request.remote}")

        return ws

