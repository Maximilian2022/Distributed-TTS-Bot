import asyncio
import json
import logging
from typing import Dict, Optional, Set, Tuple
import discord
from aiohttp import web

from src.config import AppConfig
from src.database import Database
from src.stt.dispatcher import SpeechDispatcher, GameClientConnection
import src.stt.voice_receiver

logger = logging.getLogger(__name__)

_STT_KV_KEY = "active_stt_sessions"


class STTManager:
    """早耳 Live STT & リアルタイム翻訳ストリーミングの統合統括マネージャー (マルチVC / マルチBot対応)"""

    _instance: Optional["STTManager"] = None

    def __init__(self, bot: discord.Client, db: Database, config: AppConfig):
        self.bot = bot
        self.db = db
        self.config = config

        # 後方互換マップ (guild_id -> 代表セッション)
        self.receivers: Dict[int, src.stt.voice_receiver.VoiceStreamReceiver] = {}
        self.dispatchers: Dict[int, SpeechDispatcher] = {}
        # マルチVC対応マップ ((guild_id, bot_index) -> セッション)
        self._receivers_by_pair: Dict[Tuple[int, int], src.stt.voice_receiver.VoiceStreamReceiver] = {}
        self._dispatchers_by_pair: Dict[Tuple[int, int], SpeechDispatcher] = {}

        # グローバルなゲームPCクライアント (出口B) 管理ディスパッチャ
        self.global_dispatcher = SpeechDispatcher(bot=bot, text_channel_id=None, buffer_seconds=1.0)
        STTManager._instance = self

    @classmethod
    def get_instance(cls) -> Optional["STTManager"]:
        return cls._instance

    def get_session_pair(
        self, guild_id: int, bot_index: Optional[int] = None
    ) -> Tuple[Optional[src.stt.voice_receiver.VoiceStreamReceiver], Optional[SpeechDispatcher]]:
        """指定ギルド（および任意の号機番号）に対応する (receiver, dispatcher) を取得"""
        gid = int(guild_id)
        if bot_index is not None:
            b_idx = int(bot_index)
            key = (gid, b_idx)
            if key in self._receivers_by_pair or key in self._dispatchers_by_pair:
                return self._receivers_by_pair.get(key), self._dispatchers_by_pair.get(key)
            has_any_pair_for_guild = any(k[0] == gid for k in self._receivers_by_pair) or any(
                k[0] == gid for k in self._dispatchers_by_pair
            )
            if not has_any_pair_for_guild and b_idx == 1:
                return self.receivers.get(gid), self.dispatchers.get(gid)
            return None, None
        for (k_gid, _bidx), rec in self._receivers_by_pair.items():
            if k_gid == gid and getattr(rec, "_is_running", False):
                return rec, self._dispatchers_by_pair.get((k_gid, _bidx))
        return self.receivers.get(gid), self.dispatchers.get(gid)

    def is_active(self, guild_id: int, bot_index: Optional[int] = None) -> bool:
        rec, _ = self.get_session_pair(guild_id, bot_index=bot_index)
        return bool(rec and getattr(rec, "_is_running", False))

    async def start_stt(
        self,
        guild: discord.Guild,
        text_channel_id: Optional[int] = None,
        target_user_ids: Optional[Set[int]] = None,
        trans_langs: Optional[str] = None,
        model: Optional[str] = None,
        voice_client: Optional[discord.VoiceClient] = None,
        bot_client: Optional[discord.Client] = None,
        bot_index: Optional[int] = None,
    ) -> bool:
        """指定ギルド・担当号機で STT 音声ストリーミングを開始"""
        vc: Optional[discord.VoiceClient] = voice_client or guild.voice_client
        if not vc or not vc.is_connected():
            logger.warning(f"Cannot start STT: Bot is not connected to VC in guild {guild.id}")
            return False

        bot_to_use = bot_client or getattr(vc, "client", None) or self.bot
        resolved_bot_idx = int(bot_index or getattr(bot_to_use, "bot_index", 1) or 1)

        # 同一ギルド・同一号機の既存セッションのみ停止 (別VCの別号機STTは継続)
        await self.stop_stt(guild.id, clear_persisted=False, bot_index=resolved_bot_idx)

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
        dispatcher = SpeechDispatcher(
            bot=bot_to_use,
            text_channel_id=text_channel_id,
            buffer_seconds=buf_sec,
        )
        # /stt start より先に接続していた GameBridge クライアントを新規ディスパッチャへ引き継ぎ
        async with self.global_dispatcher._game_clients_lock:
            existing_game_clients = [c for c in self.global_dispatcher._game_clients if not c.ws.closed]
        for gc_conn in existing_game_clients:
            await dispatcher.register_game_client(gc_conn.ws, gc_conn.target_user_id)

        pair_key = (int(guild.id), resolved_bot_idx)
        self._dispatchers_by_pair[pair_key] = dispatcher
        self.dispatchers[guild.id] = dispatcher

        # STT受信のため、Bot自身がスピーカーミュート (self_deaf=True) 状態なら安全に解除を試行
        try:
            me = getattr(guild, "me", None)
            if me and getattr(me, "voice", None) and getattr(me.voice, "self_deaf", False) and vc.channel:
                await guild.change_voice_state(channel=vc.channel, self_mute=False, self_deaf=False)
        except Exception as deaf_err:
            logger.debug(f"Could not unset self_deaf on STT start for guild {guild.id}: {deaf_err}")

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
            self._receivers_by_pair[pair_key] = receiver
            self.receivers[guild.id] = receiver
            ch_obj = bot_to_use.get_channel(text_channel_id) if text_channel_id else None
            ch_info = f"#{getattr(ch_obj, 'name', 'unknown')} ({text_channel_id})" if text_channel_id else "None (GamePC only)"
            logger.info(
                f"🎙️ [STTManager] Successfully started STT for guild {guild.name} ({guild.id}) [Bot #{resolved_bot_idx}] | "
                f"VC: {getattr(vc.channel, 'name', 'unknown')} ({getattr(vc.channel, 'id', None)}) | "
                f"OutputTextCh: {ch_info} | Targets: {list(target_user_ids) if target_user_ids else 'All'}"
            )
            await self._persist_session_state(
                guild_id=guild.id,
                bot_index=resolved_bot_idx,
                vc_id=getattr(vc.channel, "id", None),
                text_channel_id=text_channel_id,
                target_user_ids=target_user_ids,
                trans_langs=langs,
                model=asr_model,
            )
            return True
        else:
            await self._flush_dispatcher_immediate(dispatcher)
            self._dispatchers_by_pair.pop(pair_key, None)
            if self.dispatchers.get(guild.id) is dispatcher:
                self.dispatchers.pop(guild.id, None)
            return False

    @staticmethod
    async def _flush_dispatcher_immediate(dispatcher: SpeechDispatcher) -> None:
        """待機タイマーをキャンセルし、未送信バッファがあれば即時送信する"""
        if dispatcher._flush_task and not dispatcher._flush_task.done():
            dispatcher._flush_task.cancel()
        async with dispatcher._buffer_lock:
            if not dispatcher._buffer:
                return
            entries = list(dispatcher._buffer)
            for e in entries:
                e["flushed"] = True
            dispatcher._buffer.clear()
        await dispatcher._send_to_discord(entries)

    async def stop_stt(
        self,
        guild_id: int,
        clear_persisted: bool = True,
        bot_index: Optional[int] = None,
    ) -> None:
        """指定ギルド（および指定号機）の STT ストリーミングを停止"""
        gid = int(guild_id)
        if bot_index is not None:
            pair_keys = [(gid, int(bot_index))]
        else:
            pair_keys = [k for k in list(self._receivers_by_pair.keys()) if k[0] == gid]
            for k in list(self._dispatchers_by_pair.keys()):
                if k[0] == gid and k not in pair_keys:
                    pair_keys.append(k)

        stopped_any_pair = False
        for pk in pair_keys:
            rec = self._receivers_by_pair.pop(pk, None)
            if rec:
                stopped_any_pair = True
                await rec.stop()
                if self.receivers.get(gid) is rec:
                    self.receivers.pop(gid, None)
            disp = self._dispatchers_by_pair.pop(pk, None)
            if disp:
                stopped_any_pair = True
                await self._flush_dispatcher_immediate(disp)
                if self.dispatchers.get(gid) is disp:
                    self.dispatchers.pop(gid, None)

        # 後方互換: 直接 self.receivers[gid] にセットされていた場合
        if not stopped_any_pair and bot_index is None:
            receiver = self.receivers.pop(gid, None)
            if receiver:
                await receiver.stop()
            dispatcher = self.dispatchers.pop(gid, None)
            if dispatcher:
                await self._flush_dispatcher_immediate(dispatcher)

        # 同ギルドに別の稼働中セッションが残っていれば代表マップを再設定
        if gid not in self.receivers:
            for (k_gid, k_bidx), r_alive in self._receivers_by_pair.items():
                if k_gid == gid and getattr(r_alive, "_is_running", False):
                    self.receivers[gid] = r_alive
                    if (k_gid, k_bidx) in self._dispatchers_by_pair:
                        self.dispatchers[gid] = self._dispatchers_by_pair[(k_gid, k_bidx)]
                    break

        if clear_persisted:
            await self._clear_persisted_session_state(gid, bot_index=bot_index)

        logger.info(f"🛑 [STTManager] Stopped STT for guild {gid} (bot_index={bot_index})")

    async def stop_all(self, clear_persisted: bool = False) -> None:
        """シャットダウン時等に全稼働中 STT セッションをフラッシュ＆正常クローズ"""
        all_gids = {k[0] for k in self._receivers_by_pair.keys()} | set(self.receivers.keys())
        for gid in sorted(all_gids):
            try:
                await self.stop_stt(gid, clear_persisted=clear_persisted, bot_index=None)
            except Exception as e:
                logger.debug(f"Error stopping STT for guild {gid} during stop_all: {e}")

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
            b_idx = int(bot_index or 1)
            gid_int = int(guild_id)
            entry = {
                "guild_id": gid_int,
                "bot_index": b_idx,
                "vc_id": vc_id,
                "text_channel_id": text_channel_id,
                "target_user_ids": sorted(int(u) for u in (target_user_ids or set())),
                "trans_langs": trans_langs or "",
                "model": model or "reazon",
            }
            data[f"{gid_int}:{b_idx}"] = entry
            # 単一VC構成および既存スキーマとの後方互換代表キー
            data[str(gid_int)] = entry
            await self.db.set_system_kv(_STT_KV_KEY, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"⚠️ [STTManager] Failed to persist STT session state: {e}")

    async def _clear_persisted_session_state(self, guild_id: int, bot_index: Optional[int] = None) -> None:
        if not self.db or not hasattr(self.db, "set_system_kv"):
            return
        try:
            data = await self._load_persisted_sessions()
            changed = False
            gid_str = str(int(guild_id))
            if bot_index is not None:
                b_idx = int(bot_index)
                if data.pop(f"{gid_str}:{b_idx}", None) is not None:
                    changed = True
                old_entry = data.get(gid_str)
                if isinstance(old_entry, dict) and int(old_entry.get("bot_index", 1)) == b_idx:
                    data.pop(gid_str, None)
                    changed = True
                    # 同一ギルドで別号機のセッションが残っていれば代表キーを引き継ぐ
                    for k, v in data.items():
                        if k.startswith(f"{gid_str}:") and isinstance(v, dict):
                            data[gid_str] = v
                            break
            else:
                for k in list(data.keys()):
                    if k == gid_str or k.startswith(f"{gid_str}:"):
                        data.pop(k, None)
                        changed = True
            if changed:
                await self.db.set_system_kv(_STT_KV_KEY, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"Failed to clear persisted STT session state: {e}")

    async def restore_persisted_sessions_for_bot(self, bot_client: discord.Client, bot_index: int = 1) -> None:
        """再起動後のVC自動接続完了時に、稼働中だったSTTセッションを安全に自動復旧"""
        data = await self._load_persisted_sessions()
        if not data:
            return

        seen_pairs: Set[Tuple[int, int]] = set()
        for key_str, info in list(data.items()):
            try:
                if not isinstance(info, dict):
                    continue
                target_b_idx = int(info.get("bot_index", 1))
                if target_b_idx != int(bot_index):
                    continue
                guild_id = int(info.get("guild_id") or str(key_str).split(":")[0])
                pair_key = (guild_id, target_b_idx)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                if guild_id in self.receivers and self.is_active(guild_id, bot_index=target_b_idx):
                    continue
                guild = bot_client.get_guild(guild_id)
                if not guild or not guild.voice_client or not guild.voice_client.is_connected():
                    continue
                vc = guild.voice_client
                vc_ch = getattr(vc, "channel", None)
                human_members = [m for m in getattr(vc_ch, "members", []) if not getattr(m, "bot", False)]
                if not human_members:
                    logger.info(f"ℹ️ [STTManager] Skipping STT restore for guild {guild_id}: VC has no humans.")
                    await self._clear_persisted_session_state(guild_id, bot_index=bot_index)
                    continue

                saved_vc_id = info.get("vc_id")
                if saved_vc_id and vc_ch and vc_ch.id != int(saved_vc_id):
                    logger.info(f"ℹ️ [STTManager] Skipping STT restore for guild {guild_id}: VC changed ({saved_vc_id} -> {vc_ch.id}).")
                    await self._clear_persisted_session_state(guild_id, bot_index=bot_index)
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
                    bot_index=bot_index,
                )
                if ok:
                    logger.info(f"♻️ [STTManager] Restored persisted STT session on Bot #{bot_index} for guild {guild.name} ({guild_id})")
            except Exception as e:
                logger.warning(f"⚠️ [STTManager] Error restoring STT session for {key_str}: {e}")

    async def handle_game_ws(self, request: web.Request, forced_user_id: Optional[int] = None) -> web.WebSocketResponse:
        """ゲームPCクライアント向け WebSocket ハンドラ (/api/stt/stream)"""
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)

        if forced_user_id is not None and forced_user_id > 0:
            target_user_id = forced_user_id
        else:
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

