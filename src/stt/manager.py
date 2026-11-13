import asyncio
import json
import logging
from typing import Dict, Optional, Set
import discord
from aiohttp import web

from src.config import AppConfig
from src.database import Database
from src.stt.dispatcher import SpeechDispatcher, GameClientConnection
import src.stt.voice_receiver

logger = logging.getLogger(__name__)

_STT_KV_KEY = "active_stt_sessions"


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

        # 既存セッションがあれば停止 (再設定開始なので永続レコードは一旦残す)
        await self.stop_stt(guild.id, clear_persisted=False)

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
            ch_obj = bot_to_use.get_channel(text_channel_id) if text_channel_id else None
            ch_info = f"#{getattr(ch_obj, 'name', 'unknown')} ({text_channel_id})" if text_channel_id else "None (GamePC only)"
            logger.info(
                f"🎙️ [STTManager] Successfully started STT for guild {guild.name} ({guild.id}) | "
                f"VC: {getattr(vc.channel, 'name', 'unknown')} ({getattr(vc.channel, 'id', None)}) | "
                f"OutputTextCh: {ch_info} | Targets: {list(target_user_ids) if target_user_ids else 'All'}"
            )
            await self._persist_session_state(
                guild_id=guild.id,
                bot_index=getattr(bot_to_use, "bot_index", 1),
                vc_id=getattr(vc.channel, "id", None),
                text_channel_id=text_channel_id,
                target_user_ids=target_user_ids,
                trans_langs=langs,
                model=asr_model,
            )
            return True
        else:
            await dispatcher._scheduled_flush()
            self.dispatchers.pop(guild.id, None)
            return False

    async def stop_stt(self, guild_id: int, clear_persisted: bool = True) -> None:
        """指定ギルドの STT ストリーミングを停止"""
        receiver = self.receivers.pop(guild_id, None)
        if receiver:
            await receiver.stop()

        dispatcher = self.dispatchers.pop(guild_id, None)
        if dispatcher:
            await dispatcher._scheduled_flush()

        if clear_persisted:
            await self._clear_persisted_session_state(guild_id)

        logger.info(f"🛑 [STTManager] Stopped STT for guild {guild_id}")

    async def _load_persisted_sessions(self) -> Dict[str, dict]:
        if not self.db or not hasattr(self.db, "get_system_kv"):
            return {}
        try:
            raw = await self.db.get_system_kv(_STT_KV_KEY, "{}")
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug(f"Failed to load persisted STT sessions: {e}")
            return {}

    async def _persist_session_state(
        self,
        guild_id: int,
        bot_index: int,
        vc_id: Optional[int],
        text_channel_id: Optional[int],
        target_user_ids: Optional[Set[int]],
        trans_langs: str,
        model: str,
    ) -> None:
        if not self.db or not hasattr(self.db, "set_system_kv"):
            return
        try:
            data = await self._load_persisted_sessions()
            data[str(guild_id)] = {
                "bot_index": int(bot_index or 1),
                "vc_id": vc_id,
                "text_channel_id": text_channel_id,
                "target_user_ids": sorted(int(u) for u in (target_user_ids or set())),
                "trans_langs": trans_langs or "",
                "model": model or "reazon",
            }
            await self.db.set_system_kv(_STT_KV_KEY, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"⚠️ [STTManager] Failed to persist STT session state: {e}")

    async def _clear_persisted_session_state(self, guild_id: int) -> None:
        if not self.db or not hasattr(self.db, "set_system_kv"):
            return
        try:
            data = await self._load_persisted_sessions()
            if str(guild_id) in data:
                data.pop(str(guild_id), None)
                await self.db.set_system_kv(_STT_KV_KEY, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"Failed to clear persisted STT session state: {e}")

    async def restore_persisted_sessions_for_bot(self, bot_client: discord.Client, bot_index: int = 1) -> None:
        """再起動後のVC自動接続完了時に、稼働中だったSTTセッションを安全に自動復旧"""
        data = await self._load_persisted_sessions()
        if not data:
            return

        for gid_str, info in list(data.items()):
            try:
                if int(info.get("bot_index", 1)) != int(bot_index):
                    continue
                guild_id = int(gid_str)
                if self.is_active(guild_id):
                    continue
                guild = bot_client.get_guild(guild_id)
                if not guild or not guild.voice_client or not guild.voice_client.is_connected():
                    continue
                vc = guild.voice_client
                vc_ch = getattr(vc, "channel", None)
                human_members = [m for m in getattr(vc_ch, "members", []) if not getattr(m, "bot", False)]
                if not human_members:
                    logger.info(f"ℹ️ [STTManager] Skipping STT restore for guild {guild_id}: VC has no humans.")
                    await self._clear_persisted_session_state(guild_id)
                    continue

                saved_vc_id = info.get("vc_id")
                if saved_vc_id and vc_ch and vc_ch.id != int(saved_vc_id):
                    logger.info(f"ℹ️ [STTManager] Skipping STT restore for guild {guild_id}: VC changed ({saved_vc_id} -> {vc_ch.id}).")
                    await self._clear_persisted_session_state(guild_id)
                    continue

                targets = set(int(u) for u in (info.get("target_user_ids") or []))
                ok = await self.start_stt(
                    guild=guild,
                    text_channel_id=info.get("text_channel_id"),
                    target_user_ids=targets,
                    trans_langs=info.get("trans_langs"),
                    model=info.get("model"),
                    voice_client=vc,
                    bot_client=bot_client,
                )
                if ok:
                    logger.info(f"♻️ [STTManager] Restored persisted STT session on Bot #{bot_index} for guild {guild.name} ({guild_id})")
            except Exception as e:
                logger.warning(f"⚠️ [STTManager] Error restoring STT session for {gid_str}: {e}")

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

