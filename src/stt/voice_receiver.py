import asyncio
import logging
import math
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Set
import discord
from discord.opus import Decoder as OpusDecoder
import nacl.secret

try:
    import davey
except ImportError:
    davey = None

from src.stt.hayamimi_client import HayamimiSession
from src.stt.dispatcher import SpeechDispatcher

logger = logging.getLogger(__name__)


class VoiceStreamReceiver:
    """
    Discord VC から音声パケットを受信し、SSRCごとに完全独立デコード・独立ストリーミングして
    早耳 Live (Hayamimi) セッションへ送信する統制マネージャー
    """

    def __init__(
        self,
        voice_client: discord.VoiceClient,
        dispatcher: SpeechDispatcher,
        gateway_url: str,
        model: str = "reazon",
        trans_langs: str = "",
        gemini_key: str = "",
        token: str = "",
        target_user_ids: Optional[Set[int]] = None,  # None または空なら All (全員)
        max_speakers: int = 8,
    ):
        self.vc = voice_client
        self.dispatcher = dispatcher
        self.gateway_url = gateway_url
        self.model = model
        self.trans_langs = trans_langs
        self.gemini_key = gemini_key
        self.token = token
        self.target_user_ids: Set[int] = target_user_ids if target_user_ids is not None else set()
        self.max_speakers = max(1, min(16, max_speakers))

        # SSRC -> user_id, user_name の対応テーブル
        self._ssrc_to_user: Dict[int, int] = {}
        self._user_to_name: Dict[int, str] = {}

        # ssrc -> HayamimiSession (SSRCごとに独立セッションを保持して音声混在・VAD誤爆を100%防止)
        self._sessions: Dict[int, HayamimiSession] = {}
        # ssrc -> OpusDecoder (SSRCごとに独立デコーダーを保持してストリーム破損を防止)
        self._decoders: Dict[int, OpusDecoder] = {}

        # SSRCごとの直近音声受信時刻 & 無音送出管理
        self._last_recv_time: Dict[int, float] = {}
        self._silence_injected: Dict[int, bool] = {}

        # セッション接続中のパケット保留キュー & 接続中ロック
        self._pending_pcm: Dict[int, List[bytes]] = {}
        self._connecting_ssrcs: Set[int] = set()

        # デバッグ・運用ログ用トレーサビリティ統計
        self._rtp_recv_count = 0
        self._ssrc_pkt_counts: Dict[int, int] = {}
        self._last_stat_log_time = 0.0
        self._dave_err_logged: Set[str] = set()
        self._pushed_frame_counts: Dict[int, int] = {}
        self._last_push_log_time: Dict[int, float] = {}

        self._is_running = False
        self._lock = asyncio.Lock()
        self._silence_task: Optional[asyncio.Task] = None

        # Discord Voice WebSocket フックの保持
        self._orig_hook: Optional[Callable] = None

    async def start(self) -> bool:
        """音声受信および早耳ストリーミングを開始"""
        if self._is_running:
            return True

        if not self.vc or not self.vc.is_connected():
            logger.error("❌ VoiceClient is not connected.")
            return False

        # DAVE の Passthrough モードを有効化
        state = getattr(self.vc, "_connection", None)
        if state:
            dave = getattr(state, "dave_session", None)
            if dave and hasattr(dave, "set_passthrough_mode"):
                try:
                    dave.set_passthrough_mode(True, 120)
                except Exception as ex:
                    logger.debug(f"DAVE passthrough setup: {ex}")

        # Voice WebSocket フックの登録 (SSRC -> user_id マッピング取得用)
        self._install_ws_hook()

        # VoiceConnectionState の SocketReader にパケットリスナーを登録
        if state and hasattr(state, "add_socket_listener"):
            state.add_socket_listener(self._on_rtp_packet)
        else:
            logger.error("❌ VoiceConnectionState does not support add_socket_listener.")
            return False

        self._is_running = True

        # 発話終了時の無音パディング自動送出ループを起動
        self._silence_task = asyncio.create_task(
            self._silence_watchdog_loop(),
            name=f"stt_silence_watchdog_{self.vc.guild.id}"
        )

        # Discord Voice サーバーへの全二重受信用パイプ開放 (Speaking & 無音パケット送出)
        asyncio.create_task(
            self._prime_audio_pipe(),
            name=f"stt_prime_pipe_{self.vc.guild.id}"
        )

        logger.info(f"🎙️ [VoiceStreamReceiver] Started listening on VC for guild {self.vc.guild.id}")
        return True

    async def _prime_audio_pipe(self) -> None:
        """Discord Voice サーバーへ Speaking と無音パケットを送信して NAT パイプ・受信用ルーティングを開放"""
        try:
            logger.info(f"📡 [VoiceStreamReceiver] Priming full-duplex voice pipe to Discord server for guild {self.vc.guild.id}...")
            ws = getattr(self.vc, "ws", None)
            if ws and hasattr(ws, "speak"):
                import discord
                try:
                    await ws.speak(discord.SpeakingState.voice)
                except Exception as ws_ex:
                    logger.debug(f"ws.speak voice error: {ws_ex}")

            # 無音パケット (Opus silence: 0xf8, 0xff, 0xfe) を 5 フレーム送信
            for _ in range(5):
                if hasattr(self.vc, "send_audio_packet"):
                    try:
                        self.vc.send_audio_packet(b"\xf8\xff\xfe", encode=False)
                    except Exception as pkt_ex:
                        logger.debug(f"send_audio_packet error: {pkt_ex}")
                await asyncio.sleep(0.02)

            if ws and hasattr(ws, "speak"):
                import discord
                try:
                    await ws.speak(discord.SpeakingState.none)
                except Exception as ws_ex:
                    logger.debug(f"ws.speak none error: {ws_ex}")
            logger.info("✅ [VoiceStreamReceiver] Voice pipe primed successfully.")
        except Exception as e:
            logger.warning(f"⚠️ [VoiceStreamReceiver] Failed to prime voice pipe: {e}")

    def _install_ws_hook(self) -> None:
        """Discord Voice WebSocket から Opcode 5 (SPEAKING) や Opcode 12 (CLIENT_CONNECT) を購読"""
        state = getattr(self.vc, "_connection", None)
        if not state:
            return
        ws = getattr(state, "ws", None)
        if not ws:
            return

        # 既存のフックチェーンの再帰ループを絶対に防止するため、ws自身に元のhookを保持
        orig = getattr(ws, "_orig_ws_hook", None)
        if orig is None:
            orig = getattr(ws, "_hook", None)
            setattr(ws, "_orig_ws_hook", orig)
        self._orig_hook = orig

        async def _hook_wrapper(socket, msg):
            try:
                op = msg.get("op")
                data = msg.get("d", {})
                if op != 6:
                    logger.info(f"📡 [VoiceWS] Opcode {op} received: {data}")

                if isinstance(data, dict):
                    ssrc = data.get("ssrc") or data.get("audio_ssrc")
                    u_id_raw = data.get("user_id")
                    if ssrc and u_id_raw:
                        u_id = int(u_id_raw)
                        self._ssrc_to_user[ssrc] = u_id
                        member = self.vc.guild.get_member(u_id) if self.vc and self.vc.guild else None
                        display_name = member.display_name if member else f"User_{u_id}"
                        self._user_to_name[u_id] = display_name
                        logger.info(f"🎙️ [VoiceReceiver] SSRC {ssrc} mapped to User {u_id} ({display_name}) via WS op={op}")

                        # 既存セッションの表示名を即時昇格
                        sess = self._sessions.get(ssrc)
                        if sess:
                            sess.user_id = u_id
                            sess.user_name = display_name
            except Exception as ex:
                logger.warning(f"Error in voice ws hook: {ex}")

            try:
                if self._orig_hook and self._orig_hook != _hook_wrapper:
                    await self._orig_hook(socket, msg)
            except Exception as ex:
                logger.debug(f"Error calling original ws hook: {ex}")

        ws._hook = _hook_wrapper

    def _on_rtp_packet(self, data: bytes) -> None:
        """UDP SocketReader から届いた RTP パケットを同期処理 (DAVE復号 + SSRC独立 Opus デコード)"""
        if not self._is_running or len(data) < 16:
            return

        self._rtp_recv_count += 1
        now = time.time()

        # 5秒に1回、届いているパケット総数を確実にINFO出力
        if now - self._last_stat_log_time >= 5.0:
            self._last_stat_log_time = now
            logger.info(
                f"📊 [RTP Stats] Recv total {self._rtp_recv_count} UDP pkts | "
                f"SSRC pkt counts in session: {dict(self._ssrc_pkt_counts)} | "
                f"Target users: {list(self.target_user_ids)}"
            )

        try:
            # 1. RTP ヘッダー解析 & RTCP/無効パケット除外
            b0 = data[0]
            if (b0 >> 6) != 2:
                return

            pt = data[1] & 0x7F
            if 200 <= (data[1] & 0xFF) <= 204 or pt != 120:
                return

            ssrc = struct.unpack_from(">I", data, 8)[0]
            self._ssrc_pkt_counts[ssrc] = self._ssrc_pkt_counts.get(ssrc, 0) + 1

            if ssrc == getattr(self.vc, "ssrc", None):
                return

            # RTP ヘッダー拡張 (Extension) のスキップ
            has_ext = bool(b0 & 0x10)
            if has_ext and len(data) >= 16:
                ext_len = struct.unpack_from(">H", data, 14)[0]
                header_len = 12 + 4 + ext_len * 4
            else:
                header_len = 12

            if len(data) <= header_len:
                return

            payload = data[header_len:]

            # 2. ターゲット話者フィルタリング (SSRCが特定話者と判明している場合)
            user_id = self._ssrc_to_user.get(ssrc)
            if self.target_user_ids and user_id and user_id not in self.target_user_ids:
                return

            # 3. DAVE E2EE 復号 (Passthrough または MLS グループ鍵復号)
            state = getattr(self.vc, "_connection", None)
            dave = getattr(state, "dave_session", None) if state else None
            is_dave_active = bool(
                dave and getattr(dave, "ready", False) and getattr(dave, "protocol_version", 0) > 0 and hasattr(dave, "decrypt")
            )
            media_type_audio = getattr(davey, "MediaType", None).audio if (davey and hasattr(davey, "MediaType")) else 0

            opus_data: Optional[bytes] = None
            if is_dave_active:
                if len(self.target_user_ids) == 1:
                    target_id = next(iter(self.target_user_ids))
                    try:
                        opus_data = dave.decrypt(target_id, media_type_audio, payload)
                        if not user_id:
                            self._ssrc_to_user[ssrc] = target_id
                            user_id = target_id
                            member = self.vc.guild.get_member(target_id) if self.vc and self.vc.guild else None
                            if member:
                                self._user_to_name[target_id] = member.display_name
                    except Exception as ex:
                        err_key = f"dave_err_{ssrc}_{type(ex).__name__}"
                        if err_key not in self._dave_err_logged:
                            self._dave_err_logged.add(err_key)
                            logger.info(f"⚠️ [DAVE] Decrypt error on SSRC {ssrc}: {ex}")
                        return
                else:
                    uid_to_try = user_id
                    if not uid_to_try:
                        uids = getattr(dave, "get_user_ids", lambda: [])()
                        cand = [u for u in uids if u != getattr(self.vc, "user_id", None) and (not self.vc.guild.me or u != self.vc.guild.me.id)]
                        uid_to_try = cand[0] if cand else (uids[0] if uids else None)

                    if uid_to_try:
                        try:
                            opus_data = dave.decrypt(uid_to_try, media_type_audio, payload)
                        except Exception as ex:
                            err_key = f"dave_err_{ssrc}_{type(ex).__name__}"
                            if err_key not in self._dave_err_logged:
                                self._dave_err_logged.add(err_key)
                                logger.info(f"⚠️ [DAVE] Decrypt error on SSRC {ssrc}: {ex}")
                            return
                    else:
                        opus_data = payload
            else:
                opus_data = payload

            if not opus_data:
                return

            # 4. Opus デコード (SSRC ごとに完全独立したデコーダーを使用 & 破損時自己修復)
            decoder = self._decoders.get(ssrc)
            if not decoder:
                decoder = OpusDecoder()
                self._decoders[ssrc] = decoder

            try:
                pcm = decoder.decode(opus_data)
            except Exception as dec_err:
                # デコーダーの予測状態が壊れた場合は即座に再生成して自己修復
                self._decoders[ssrc] = OpusDecoder()
                err_key = f"dec_{ssrc}"
                if err_key not in self._dave_err_logged:
                    self._dave_err_logged.add(err_key)
                    logger.warning(f"❌ [VoiceReceiver] Opus decode error on SSRC {ssrc}: {dec_err} (decoder reset)")
                return

            if not pcm:
                return

            # 音声レベル (RMS) の計算 (3840 bytes = 960 stereo samples)
            shorts = struct.unpack(f"<{len(pcm)//2}h", pcm)
            sum_sq = sum(s * s for s in shorts)
            rms = math.sqrt(sum_sq / len(shorts)) if shorts else 0.0

            # 5. SSRC 専用の早耳 Live セッションへの送信 (音声混在を物理的に遮断)
            session = self._sessions.get(ssrc) or (self._sessions.get(user_id) if user_id else None)
            if session and session.is_connected:
                session.push_audio(pcm)
                self._last_recv_time[ssrc] = now
                self._silence_injected[ssrc] = False

                p_cnt = self._pushed_frame_counts.get(ssrc, 0) + 1
                self._pushed_frame_counts[ssrc] = p_cnt
                last_push_log = self._last_push_log_time.get(ssrc, 0.0)

                is_voice = rms >= 100.0
                if now - last_push_log >= 3.0 or p_cnt == 1 or (is_voice and now - last_push_log >= 1.0):
                    self._last_push_log_time[ssrc] = now
                    level_tag = "🗣️ Voice" if is_voice else "🔈 Low/Noise"
                    logger.info(
                        f"🎙️ [VoiceReceiver] Stream -> Hayamimi: {session.user_name} (SSRC: {ssrc}) "
                        f"| {level_tag} RMS={rms:.1f} | Frame #{p_cnt} ({p_cnt*20}ms sent)"
                    )
            else:
                buf = self._pending_pcm.setdefault(ssrc, [])
                if len(buf) < 150:
                    buf.append(pcm)
                if ssrc not in self._connecting_ssrcs:
                    self._connecting_ssrcs.add(ssrc)
                    logger.info(f"⏳ [VoiceReceiver] Connecting session for SSRC {ssrc}, buffering frame #{len(buf)} (RMS={rms:.1f})")
                    asyncio.run_coroutine_threadsafe(
                        self._get_or_create_ssrc_session(ssrc, user_id),
                        self.vc.loop
                    )

        except Exception as e:
            logger.debug(f"Error handling RTP packet: {e}")

    async def _silence_watchdog_loop(self) -> None:
        """
        発話終了検知ウォッチドッグ:
        SSRC ごとにパケットが途切れた際 (最後の音声から 120ms 経過)、
        早耳 Live の Silero VAD (無音 350ms で発話確定) に確実に確定させるため、
        無音フレーム (500ms 分 = 25 フレーム) を自動送出する。
        """
        silence_chunk = b"\x00" * 3840  # 20ms = 3840 bytes (48kHz Stereo 16-bit)
        last_heartbeat_time = 0.0
        try:
            while self._is_running:
                await asyncio.sleep(0.04)
                now = time.time()

                # 10秒に1回、VC受信状況・VC参加者・セッション状態を詳細サマリー出力
                if now - last_heartbeat_time >= 10.0:
                    last_heartbeat_time = now
                    ch = self.vc.channel if self.vc else None
                    human_members = [
                        f"{m.display_name}({m.id})"
                        for m in (ch.members if ch else [])
                        if not getattr(m, "bot", False)
                    ]
                    active_sess = [
                        f"{s.user_name}(SSRC:{ssrc})"
                        for ssrc, s in self._sessions.items()
                        if s.is_connected
                    ]
                    targets_str = "All (全員)" if not self.target_user_ids else list(self.target_user_ids)
                    logger.info(
                        f"💓 [STT Heartbeat] VC: '{getattr(ch, 'name', 'N/A')}' | "
                        f"VC Humans: {human_members} | "
                        f"Target: {targets_str} | "
                        f"RTP Pkts: {self._rtp_recv_count} | "
                        f"Mapped SSRCs: {dict(self._ssrc_to_user)} | "
                        f"Active Hayamimi Streams: {active_sess}"
                    )

                for ssrc, session in list(self._sessions.items()):
                    if not session.is_connected:
                        continue
                    last_time = self._last_recv_time.get(ssrc, 0.0)
                    injected = self._silence_injected.get(ssrc, True)

                    # 最後の音声から 120ms 経過し、まだ無音を送出していない場合
                    if not injected and (now - last_time) >= 0.12:
                        elapsed_ms = (now - last_time) * 1000
                        logger.info(
                            f"🔇 [SilenceWatchdog] Speech pause detected for {session.user_name} (SSRC {ssrc}) "
                            f"(gap: {elapsed_ms:.0f}ms). Injecting 25 silence frames (500ms) to trigger Silero VAD..."
                        )
                        for _ in range(25):
                            session.push_audio(silence_chunk)
                        self._silence_injected[ssrc] = True
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Error in silence watchdog: {e}")

    async def _get_or_create_ssrc_session(self, ssrc: int, user_id: Optional[int] = None) -> Optional[HayamimiSession]:
        """SSRC 専用の早耳 Live WebSocket セッションを取得または新規作成"""
        async with self._lock:
            if ssrc in self._sessions:
                sess = self._sessions[ssrc]
                if user_id and sess.user_id != user_id:
                    sess.user_id = user_id
                    member = self.vc.guild.get_member(user_id) if self.vc and self.vc.guild else None
                    if member:
                        sess.user_name = member.display_name
                return sess

            if len(self._sessions) >= self.max_speakers:
                logger.warning(f"⚠️ [Hayamimi] Reached max simultaneous speaker sessions ({self.max_speakers})")
                return None

            resolved_uid = user_id
            resolved_name = None
            if resolved_uid:
                member = self.vc.guild.get_member(resolved_uid) if self.vc and self.vc.guild else None
                resolved_name = member.display_name if member else f"User_{resolved_uid}"
            elif len(self.target_user_ids) == 1:
                resolved_uid = next(iter(self.target_user_ids))
                member = self.vc.guild.get_member(resolved_uid) if self.vc and self.vc.guild else None
                resolved_name = member.display_name if member else f"User_{resolved_uid}"
            else:
                # まだ user_id が特定されていない場合、VCの人間メンバーから未割当のメンバーを推測、または Speaker_{ssrc}
                resolved_uid = ssrc
                resolved_name = f"Speaker_{ssrc}"

            session = HayamimiSession(
                user_id=resolved_uid,
                user_name=resolved_name,
                gateway_url=self.gateway_url,
                model=self.model,
                trans_langs=self.trans_langs,
                gemini_key=self.gemini_key,
                token=self.token,
                on_transcript=self.dispatcher.on_transcript,
                on_translation=self.dispatcher.on_translation,
                loop=self.vc.loop,
            )
            success = await session.start()
            if success:
                self._sessions[ssrc] = session
                logger.info(f"🎙️ [VoiceReceiver] Started dedicated Hayamimi stream for SSRC {ssrc} ({resolved_name})")

                # 保留されていたパケットを即時全量フラッシュ
                pending = self._pending_pcm.pop(ssrc, [])
                if pending:
                    logger.info(f"🚀 [VoiceReceiver] Flushing {len(pending)} buffered frames to SSRC {ssrc} ({resolved_name})")
                    for chunk in pending:
                        session.push_audio(chunk)
                    self._last_recv_time[ssrc] = time.time()
                    self._silence_injected[ssrc] = False

                self._connecting_ssrcs.discard(ssrc)
                return session

            self._connecting_ssrcs.discard(ssrc)
            self._pending_pcm.pop(ssrc, None)
            return None

    async def stop(self) -> None:
        """音声受信および全セッションのクリーンアップ"""
        if not self._is_running:
            return
        self._is_running = False

        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()

        state = getattr(self.vc, "_connection", None)
        if state and hasattr(state, "remove_socket_listener"):
            try:
                state.remove_socket_listener(self._on_rtp_packet)
            except Exception:
                pass

        # Voice WebSocket フックを元の実装に安全に復元
        ws = getattr(state, "ws", None) if state else None
        if ws and hasattr(ws, "_orig_ws_hook"):
            try:
                ws._hook = ws._orig_ws_hook
            except Exception:
                pass

        # 全早耳セッションの切断
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for s in sessions:
            await s.close()

        self._decoders.clear()
        self._last_recv_time.clear()
        self._silence_injected.clear()
        self._pending_pcm.clear()
        self._connecting_ssrcs.clear()
        logger.info(f"🛑 [VoiceStreamReceiver] Stopped listening on VC for guild {self.vc.guild.id}")
