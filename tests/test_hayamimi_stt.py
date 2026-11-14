import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import AppConfig
from src.stt.hayamimi_client import HayamimiSession
from src.stt.dispatcher import SpeechDispatcher, GameClientConnection


class TestHayamimiSTT(unittest.IsolatedAsyncioTestCase):
    def test_hayamimi_session_url_builder(self):
        """HayamimiSession の WebSocket URL 生成テスト"""
        session = HayamimiSession(
            user_id=123456789,
            user_name="TestUser",
            gateway_url="ws://192.168.11.244:8833/ws/stream",
            model="reazon",
            trans_langs="en,ko",
            gemini_key="test_gemini_key",
            token="test_token",
        )
        url = session.build_ws_url()
        self.assertIn("ws://192.168.11.244:8833/ws/stream?", url)
        self.assertIn("sample_rate=48000", url)
        self.assertIn("channels=2", url)
        self.assertIn("format=s16le", url)
        self.assertIn("model=reazon", url)
        self.assertIn("trans_langs=en%2Cko", url)
        self.assertIn("gemini_key=test_gemini_key", url)
        self.assertIn("token=test_token", url)

    def test_config_hayamimi_fields(self):
        """AppConfig の Hayamimi 設定と DB クレデンシャル適用テスト"""
        cfg = AppConfig()
        self.assertEqual(cfg.hayamimi_model, "reazon")
        self.assertEqual(cfg.hayamimi_buffer_seconds, 1.0)
        self.assertEqual(cfg.hayamimi_max_speakers, 8)

        # apply_db_credentials による更新
        creds = {
            "hayamimi_ws_url": "ws://example.com/ws/stream",
            "hayamimi_model": "haas",
            "hayamimi_trans_langs": "en",
            "hayamimi_buffer_seconds": "1.5",
            "hayamimi_max_speakers": "4",
        }
        cfg.apply_db_credentials(creds, [])
        self.assertEqual(cfg.hayamimi_ws_url, "ws://example.com/ws/stream")
        self.assertEqual(cfg.hayamimi_model, "haas")
        self.assertEqual(cfg.hayamimi_trans_langs, "en")
        self.assertEqual(cfg.hayamimi_buffer_seconds, 1.5)
        self.assertEqual(cfg.hayamimi_max_speakers, 4)

    async def test_dispatcher_buffering_and_rate_limit(self):
        """SpeechDispatcher のバッファリング結合とレートリミット保護テスト"""
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel

        dispatcher = SpeechDispatcher(bot=mock_bot, text_channel_id=9999, buffer_seconds=0.05)
        dispatcher.buffer_seconds = 0.05  # テスト用に短縮
        dispatcher._min_send_interval = 0.01

        # 2件の発話を連続投入
        await dispatcher.on_transcript(1001, "UserA", {"text": "こんにちは", "is_final": True, "seq": 1})
        await dispatcher.on_transcript(1002, "UserB", {"text": "お疲れ様です", "is_final": True, "seq": 1})

        # フラッシュタスクの完了を待機
        if dispatcher._flush_task:
            await dispatcher._flush_task

        # 1回の send で2行が結合されて送信されているか検証
        self.assertEqual(mock_channel.send.call_count, 1)
        sent_content = mock_channel.send.call_args[0][0]
        self.assertIn("🎙️ **UserA**: こんにちは", sent_content)
        self.assertIn("🎙️ **UserB**: お疲れ様です", sent_content)

    async def test_dispatcher_translation_inline_merging(self):
        """字幕と翻訳テキストが正しくインライン統合されるか検証"""
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel

        dispatcher = SpeechDispatcher(bot=mock_bot, text_channel_id=9999, buffer_seconds=0.05)
        dispatcher.buffer_seconds = 0.05
        dispatcher._min_send_interval = 0.01

        # 字幕投入
        await dispatcher.on_transcript(1001, "UserA", {"text": "ありがとう", "is_final": True, "seq": 42})
        # 直後に同一 seq の翻訳投入
        await dispatcher.on_translation(1001, "UserA", {"text": "Thank you", "seq": 42})

        # フラッシュタスクの完了を待機
        if dispatcher._flush_task:
            await dispatcher._flush_task

        self.assertEqual(mock_channel.send.call_count, 1)
        sent_content = mock_channel.send.call_args[0][0]
        self.assertIn("🎙️ **UserA**: ありがとう (Thank you)", sent_content)

    async def test_game_client_instant_dispatch(self):
        """ゲームPCクライアントへは Discord の待機なしで即座に送信されるか検証"""
        mock_bot = MagicMock()
        mock_ws = AsyncMock()
        mock_ws.closed = False

        dispatcher = SpeechDispatcher(bot=mock_bot, text_channel_id=None, buffer_seconds=5.0)
        conn = await dispatcher.register_game_client(mock_ws, target_user_id=1001)

        # UserA の発話
        await dispatcher.on_transcript(1001, "UserA", {"text": "ポーション使って！", "is_final": True, "seq": 10})

        # mock_ws.send_json が即時呼ばれたか検証 (5秒待機を待たずに即時)
        self.assertEqual(mock_ws.send_json.call_count, 1)
        payload = mock_ws.send_json.call_args[0][0]
        self.assertEqual(payload["type"], "game_chat")
        self.assertEqual(payload["text"], "ポーション使って！")
        self.assertEqual(payload["user_name"], "UserA")
        self.assertEqual(payload["user_id"], 1001)

        await dispatcher.unregister_game_client(conn)

    async def test_voice_receiver_dave_and_speaker_isolation(self):
        """VoiceStreamReceiver の DAVE E2EE 復号およびターゲット話者分離テスト"""
        from src.stt.voice_receiver import VoiceStreamReceiver
        import struct

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.guild.id = 12345
        mock_vc.ssrc = 9999  # Bot自身の SSRC

        # DAVE セッションのモック
        mock_dave = MagicMock()
        mock_dave.ready = True
        mock_dave.protocol_version = 1
        mock_dave.get_user_ids.return_value = [1001, 2002]

        # 1001 (ターゲット) かつターゲット自身の payload のみ正常復号
        def fake_decrypt(uid, media_type, payload):
            if uid == 1001 and payload == b"\xaa\xbb\xcc\xdd":
                return b"\xf8\xff\xfe"  # ダミー Opus
            raise Exception("Cannot decrypt: AEAD tag mismatch")

        mock_dave.decrypt.side_effect = fake_decrypt

        mock_conn = MagicMock()
        mock_conn.dave_session = mock_dave
        mock_vc._connection = mock_conn

        mock_dispatcher = MagicMock()
        receiver = VoiceStreamReceiver(
            voice_client=mock_vc,
            dispatcher=mock_dispatcher,
            gateway_url="ws://dummy/ws/stream",
            target_user_ids={1001},  # 1001 のみをターゲットに指定
        )
        receiver._is_running = True

        # モックの OpusDecoder を仕込む
        mock_decoder = MagicMock()
        mock_decoder.decode.return_value = b"\x00" * 3840
        receiver._decoders[8888] = mock_decoder

        # ターゲット話者 (SSRC 8888) の RTP パケット作成 (Header 12 bytes + payload)
        # b0 = 0x80 (v=2), pt = 120, ssrc = 8888
        pkt_target = bytearray(12)
        pkt_target[0] = 0x80
        pkt_target[1] = 120
        struct.pack_into(">I", pkt_target, 8, 8888)
        pkt_target += b"\xaa\xbb\xcc\xdd"  # payload

        # 早耳セッションのモック
        mock_session = MagicMock()
        receiver._sessions[1001] = mock_session

        # ターゲットのパケットを処理
        receiver._on_rtp_packet(bytes(pkt_target))

        # SSRC 8888 が 1001 にマッピングされ、セッションに音声が届いたか検証
        self.assertEqual(receiver._ssrc_to_user.get(8888), 1001)
        mock_session.push_audio.assert_called_once_with(b"\x00" * 3840)

        # 続いて、ターゲット外 (SSRC 7777, 別人 2002) のパケット
        pkt_other = bytearray(12)
        pkt_other[0] = 0x80
        pkt_other[1] = 120
        struct.pack_into(">I", pkt_other, 8, 7777)
        pkt_other += b"\x11\x22\x33\x44"

        mock_session.reset_mock()
        receiver._on_rtp_packet(bytes(pkt_other))

        # ターゲット外なので push_audio は呼ばれない
        mock_session.push_audio.assert_not_called()
        self.assertNotIn(7777, receiver._ssrc_to_user)

    async def test_voice_receiver_transport_aead_rtpsize_decryption(self):
        """Discord Voice Transport 暗号化 (aead_xchacha20_poly1305_rtpsize + RTP拡張) の完全復号テスト"""
        from src.stt.voice_receiver import VoiceStreamReceiver
        import nacl.secret
        import struct

        secret_key = list(range(32))
        box = nacl.secret.Aead(bytes(secret_key))

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.guild.id = 12345
        mock_vc.ssrc = 9999

        mock_conn = MagicMock()
        mock_conn.dave_session = None
        mock_conn.mode = "aead_xchacha20_poly1305_rtpsize"
        mock_conn.secret_key = secret_key
        mock_vc._connection = mock_conn

        mock_dispatcher = MagicMock()
        receiver = VoiceStreamReceiver(
            voice_client=mock_vc,
            dispatcher=mock_dispatcher,
            gateway_url="ws://dummy/ws/stream",
            target_user_ids={1001},
        )
        receiver._is_running = True

        mock_decoder = MagicMock()
        mock_decoder.decode.return_value = b"\x01\x02" * 1920
        receiver._decoders[5627] = mock_decoder

        mock_session = MagicMock()
        mock_session.is_connected = True
        receiver._sessions[5627] = mock_session

        # RTP Header Extension あり (b0=0x90, pt=120, ext profile=0xBEDE, len=1 word)
        header = struct.pack(">BBHIIHH", 0x90, 120, 10, 4800, 5627, 0xBEDE, 1)
        ext_body = b"\x10\x45\x00\x00"
        plain_opus = b"\xf8\xff\xfe\xaa\xbb"
        nonce = bytearray(24)
        nonce[:4] = struct.pack(">I", 77)
        encrypted_pkt = header + box.encrypt(ext_body + plain_opus, header, bytes(nonce)).ciphertext + nonce[:4]

        receiver._on_rtp_packet(encrypted_pkt)

        # 拡張ヘッダが正しく除去され、生の Opus パケットだけがデコーダに渡されたか検証
        mock_decoder.decode.assert_called_once_with(plain_opus)
        mock_session.push_audio.assert_called_once_with(b"\x01\x02" * 1920)
        self.assertEqual(receiver._decoded_frame_count, 1)

    async def test_voice_receiver_ws_hook_closes_nontarget_session(self):
        """WS Opcode 5 で後からターゲット外話者と判明した場合に誤セッションを即座に破棄するテスト"""
        from src.stt.voice_receiver import VoiceStreamReceiver

        mock_ws = MagicMock()
        mock_ws._hook = AsyncMock()
        mock_conn = MagicMock()
        mock_conn.ws = mock_ws
        mock_vc = MagicMock()
        mock_vc.guild.get_member.return_value = MagicMock(display_name="OtherUser")
        mock_vc._connection = mock_conn

        receiver = VoiceStreamReceiver(
            voice_client=mock_vc,
            dispatcher=MagicMock(),
            gateway_url="ws://dummy/ws/stream",
            target_user_ids={1001},
        )
        receiver._install_ws_hook()

        # 事前接続されてしまったターゲット外 SSRC 5654 のダミーセッション
        wrong_sess = MagicMock()
        wrong_sess.close = AsyncMock()
        receiver._sessions[5654] = wrong_sess

        # Opcode 5 で SSRC 5654 = user_id 2002 (ターゲット外) が通知される
        await mock_ws._hook(None, {"op": 5, "d": {"user_id": "2002", "ssrc": 5654, "speaking": 1}})
        await asyncio.sleep(0)

        self.assertNotIn(5654, receiver._sessions)
        wrong_sess.close.assert_awaited_once()

    async def test_stt_manager_persist_and_restore(self):
        """STTManager のセッション状態永続化と再起動後自動復元テスト"""
        from src.stt.manager import STTManager
        import json

        kv_store = {}
        mock_db = MagicMock()
        mock_db.get_system_kv = AsyncMock(side_effect=lambda k, default=None: kv_store.get(k, default))
        mock_db.set_system_kv = AsyncMock(side_effect=lambda k, v: kv_store.__setitem__(k, v))

        cfg = AppConfig()
        cfg.hayamimi_ws_url = "ws://127.0.0.1:8833/ws/stream"

        mock_bot = MagicMock()
        mock_bot.bot_index = 1
        mock_guild = MagicMock()
        mock_guild.id = 999
        mock_guild.name = "TestGuild"
        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.channel.id = 555
        mock_vc.channel.name = "GeneralVC"
        mock_vc.channel.members = [MagicMock(bot=False)]
        mock_guild.voice_client = mock_vc
        mock_bot.get_guild.return_value = mock_guild

        mgr = STTManager(bot=mock_bot, db=mock_db, config=cfg)

        async def fake_start(self_rec):
            self_rec._is_running = True
            return True

        with patch("src.stt.voice_receiver.VoiceStreamReceiver.start", new=fake_start), \
             patch("src.stt.voice_receiver.VoiceStreamReceiver.stop", new=AsyncMock()):
            ok = await mgr.start_stt(
                guild=mock_guild,
                text_channel_id=777,
                target_user_ids={1001},
                trans_langs="en",
                voice_client=mock_vc,
                bot_client=mock_bot,
            )
            self.assertTrue(ok)
            self.assertIn("active_stt_sessions", kv_store)
            saved = json.loads(kv_store["active_stt_sessions"])
            self.assertEqual(saved["999"]["text_channel_id"], 777)
            self.assertEqual(saved["999"]["target_user_ids"], [1001])

            # 再起動をシミュレート (メモリ上の receivers クリア)
            mgr.receivers.clear()
            mgr.dispatchers.clear()
            await mgr.restore_persisted_sessions_for_bot(mock_bot, bot_index=1)
            self.assertTrue(mgr.is_active(999))
            self.assertEqual(mgr.dispatchers[999].text_channel_id, 777)

            # /stt stop で永続化状態も削除されるか
            await mgr.stop_stt(999, clear_persisted=True)
            saved_after = json.loads(kv_store["active_stt_sessions"])
            self.assertNotIn("999", saved_after)


class TestHayamimiTranslation(unittest.IsolatedAsyncioTestCase):
    def test_translate_language_constants_and_helpers(self):
        """8言語定義・URL導出・STT複数言語解決・前処理ヘルパーの検証"""
        from src.stt.hayamimi_client import (
            HAYAMIMI_TRANSLATE_LANGUAGES,
            HAYAMIMI_VALID_LANG_CODES,
            normalize_hayamimi_lang,
            prepare_text_for_translation,
            resolve_hayamimi_translate_url,
            resolve_stt_trans_langs,
        )

        expected_codes = {"en", "zh", "zh-tw", "ko", "es", "fr", "de", "pt"}
        self.assertEqual(HAYAMIMI_VALID_LANG_CODES, expected_codes)
        self.assertEqual(len(HAYAMIMI_TRANSLATE_LANGUAGES), 8)

        self.assertEqual(normalize_hayamimi_lang("ZH-TW"), "zh-tw")
        self.assertEqual(normalize_hayamimi_lang("invalid", default="en"), "en")

        self.assertEqual(
            resolve_hayamimi_translate_url("ws://192.0.2.10:8833/ws/stream"),
            "http://192.0.2.10:8833/api/translate",
        )
        self.assertEqual(
            resolve_hayamimi_translate_url("wss://example.com/ws/stream"),
            "https://example.com/api/translate",
        )
        self.assertEqual(resolve_hayamimi_translate_url(""), "")

        self.assertEqual(resolve_stt_trans_langs(None, None, "en,ko"), "en,ko")
        self.assertEqual(resolve_stt_trans_langs("none", None, "en,ko"), "")
        self.assertEqual(resolve_stt_trans_langs("en", None, "ja"), "en")
        self.assertEqual(resolve_stt_trans_langs("en", "ko", ""), "en,ko")
        self.assertEqual(resolve_stt_trans_langs("zh-tw", "zh-tw", ""), "zh-tw")

        mock_guild = MagicMock()
        mock_member = MagicMock(display_name="Taro")
        mock_guild.get_member.return_value = mock_member
        self.assertEqual(
            prepare_text_for_translation("<@12345> おはよう https://example.com <:wave:999>", mock_guild),
            "@Taro おはよう",
        )
        self.assertIsNone(prepare_text_for_translation("https://example.com", mock_guild))
        self.assertIsNone(prepare_text_for_translation("123456", mock_guild))
        self.assertIsNone(prepare_text_for_translation("👍👍", mock_guild))
        self.assertEqual(prepare_text_for_translation("안녕하세요", mock_guild), "안녕하세요")

    async def test_hayamimi_translator_http_post(self):
        """HayamimiTranslator が /api/translate へ POST しレスポンスを処理するテスト"""
        from src.stt.hayamimi_client import HayamimiTranslator

        cfg = AppConfig()
        cfg.hayamimi_ws_url = "ws://127.0.0.1:8833/ws/stream"
        translator = HayamimiTranslator(cfg)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "ok": True,
            "source_lang": "ja",
            "original": "こんにちは",
            "translations": {"en": "Hello"},
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post.return_value = mock_resp
        mock_session.close = AsyncMock()

        with patch.object(translator, "_get_session", new=AsyncMock(return_value=mock_session)):
            res = await translator.translate("こんにちは", target_langs=["en"])
            self.assertIsNotNone(res)
            self.assertEqual(res["translations"]["en"], "Hello")
            mock_session.post.assert_called_once()
            called_url = mock_session.post.call_args[0][0]
            self.assertEqual(called_url, "http://127.0.0.1:8833/api/translate")

            # 例外発生時も落ちずに None を返す
            mock_session.post.side_effect = RuntimeError("connection refused")
            res_err = await translator.translate("こんにちは", target_langs=["en"])
            self.assertIsNone(res_err)

        await translator.close()

    async def test_database_tts_translate_persistence(self):
        """guild_settings の tts_translate_enabled / tts_translate_lang 永続化テスト"""
        import tempfile
        import os
        from src.database import Database

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_trans.db")
            db = Database(db_path)
            await db.init_db()
            try:
                default_cfg = await db.get_guild_settings(1111)
                self.assertEqual(default_cfg["tts_translate_enabled"], 0)
                self.assertEqual(default_cfg["tts_translate_lang"], "en")

                updated = await db.update_guild_settings(
                    1111, tts_translate_enabled=1, tts_translate_lang="zh-tw"
                )
                self.assertEqual(updated["tts_translate_enabled"], 1)
                self.assertEqual(updated["tts_translate_lang"], "zh-tw")

                # キャッシュ破棄してDBから再読込できることを検証
                db.invalidate_guild_cache(1111)
                reloaded = await db.get_guild_settings(1111)
                self.assertEqual(reloaded["tts_translate_enabled"], 1)
                self.assertEqual(reloaded["tts_translate_lang"], "zh-tw")

                # 不正言語コード時は "en" に安全フォールバック
                sanitized = await db.update_guild_settings(1111, tts_translate_lang="invalid_xx")
                self.assertEqual(sanitized["tts_translate_lang"], "en")
            finally:
                await db.close()

    async def test_vc_handler_translate_and_reply_and_ng_guard(self):
        """VCHandlerCog の双方向翻訳リプライ表示および NGワード・URLスキップ検証"""
        import discord
        from src.cogs.vc_handler import VCHandlerCog

        cfg = AppConfig()
        cfg.hayamimi_ws_url = "ws://127.0.0.1:8833/ws/stream"
        mock_bot = MagicMock()
        mock_db = MagicMock()
        mock_cache = MagicMock()
        mock_tts = MagicMock()

        cog = VCHandlerCog(mock_bot, cfg, mock_db, mock_cache, mock_tts)
        try:
            # 1. 日本語 -> 指定言語 (ko)
            msg_ja = MagicMock(spec=discord.Message)
            msg_ja.id = 1
            msg_ja.content = "よろしくお願いします"
            msg_ja.guild = MagicMock()
            msg_ja.guild.get_member.return_value = None
            msg_ja.reply = AsyncMock()

            cog.hayamimi_translator.translate = AsyncMock(return_value={
                "ok": True,
                "source_lang": "ja",
                "translations": {"ko": "잘 부탁드립니다."},
            })
            await cog._translate_and_reply_message(msg_ja, "ko")
            msg_ja.reply.assert_awaited_once()
            self.assertEqual(msg_ja.reply.call_args[0][0], "🌐 `[KO]` 잘 부탁드립니다.")
            self.assertFalse(msg_ja.reply.call_args.kwargs["mention_author"])

            # 2. 他言語 (en) -> 自動日本語 ("ja")
            msg_en = MagicMock(spec=discord.Message)
            msg_en.id = 2
            msg_en.content = "Good morning!"
            msg_en.guild = MagicMock()
            msg_en.reply = AsyncMock()

            cog.hayamimi_translator.translate = AsyncMock(return_value={
                "ok": True,
                "source_lang": "en",
                "translations": {"ja": "おはようございます！"},
            })
            await cog._translate_and_reply_message(msg_en, "ko")
            self.assertEqual(msg_en.reply.call_args[0][0], "🌐 `[JA]` おはようございます！")

            # 3. URLのみは API 呼び出し自体をスキップ
            msg_url = MagicMock(spec=discord.Message)
            msg_url.id = 3
            msg_url.content = "https://example.com/abc"
            msg_url.guild = MagicMock()
            msg_url.reply = AsyncMock()
            cog.hayamimi_translator.translate.reset_mock()
            await cog._translate_and_reply_message(msg_url, "en")
            cog.hayamimi_translator.translate.assert_not_awaited()
            msg_url.reply.assert_not_awaited()

            # 4. on_message で NG ワード含有時は翻訳タスクを起こさず、通常時は起こす
            state = cog.get_guild_state(100)
            state.bound_text_channel_id = 200
            state.is_playing = False
            mock_vc = MagicMock()
            mock_vc.is_connected.return_value = True
            mock_guild = MagicMock()
            mock_guild.id = 100
            mock_guild.name = "TestGuild"
            mock_guild.voice_client = mock_vc

            mock_db.get_guild_settings = AsyncMock(return_value={
                "ignore_prefixes": [";"],
                "max_text_length": 50,
                "read_username": 0,
                "dedup_window_seconds": 5.0,
                "dedup_max_length": 10,
                "speculative_timeout_seconds": 1.5,
                "tts_translate_enabled": 1,
                "tts_translate_lang": "en",
            })
            mock_db.get_dictionary_words = AsyncMock(return_value=[])
            mock_db.get_ng_words = AsyncMock(return_value=["badword"])
            mock_db.touch_ng_words = AsyncMock()
            mock_db.get_or_create_user_settings = AsyncMock(return_value={
                "speaker_id": "nanami",
                "pitch": "0%",
                "speed": 1.0,
                "volume": 0.8,
            })

            def _make_msg(content: str, mid: int) -> MagicMock:
                m = MagicMock(spec=discord.Message)
                m.id = mid
                m.content = content
                m.guild = mock_guild
                m.channel = MagicMock(id=200)
                m.author = MagicMock(id=555, bot=False, display_name="UserA")
                m.created_at = None
                m.reply = AsyncMock()
                return m

            with patch.object(cog, "_playback_loop", new=AsyncMock()), \
                 patch.object(cog, "_translate_and_reply_message", new=AsyncMock()) as mock_trans_task, \
                 patch("src.cogs.vc_handler.safe_create_task", side_effect=lambda coro, **kw: asyncio.create_task(coro)):
                await cog.on_message(_make_msg("badword です", 10))
                await asyncio.sleep(0)
                mock_trans_task.assert_not_awaited()

                await cog.on_message(_make_msg("通常のテスト発言", 11))
                await asyncio.sleep(0)
                mock_trans_task.assert_awaited_once()
                self.assertEqual(mock_trans_task.call_args[0][1], "en")
        finally:
            await cog.hayamimi_translator.close()

    async def test_commands_choices_and_translate_commands(self):
        """CommandCog の STT 選択肢定義と /translate コマンド群の検証"""
        import discord
        from src.cogs.commands import (
            CommandsCog,
            _STT_LANG_1_CHOICES,
            _STT_LANG_2_CHOICES,
            _TTS_TRANSLATE_LANG_CHOICES,
        )

        self.assertEqual(len(_TTS_TRANSLATE_LANG_CHOICES), 8)
        self.assertEqual(len(_STT_LANG_1_CHOICES), 9)
        self.assertEqual(len(_STT_LANG_2_CHOICES), 9)
        self.assertEqual([c.value for c in _TTS_TRANSLATE_LANG_CHOICES], ["en", "zh", "zh-tw", "ko", "es", "fr", "de", "pt"])

        cfg = AppConfig()
        mock_db = MagicMock()
        mock_db.get_guild_settings = AsyncMock(return_value={
            "admin_role_id": 0,
            "tts_translate_enabled": 1,
            "tts_translate_lang": "zh-tw",
            "auto_join_pairs": [
                {"vc_id": 10, "text_id": 20, "translate_enabled": True, "translate_lang": "ko"}
            ],
        })
        mock_db.update_guild_settings = AsyncMock(return_value={
            "tts_translate_enabled": 1,
            "tts_translate_lang": "zh-tw",
        })

        cog = CommandsCog(MagicMock(), cfg, mock_db, MagicMock(), MagicMock())
        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild_id = 123
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.roles = []
        interaction.user.guild_permissions = MagicMock(administrator=True, manage_guild=True)
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()

        await cog.translate_set.callback(cog, interaction, enabled=True, target_lang="zh-tw")
        mock_db.update_guild_settings.assert_awaited_once_with(
            123, tts_translate_enabled=1, tts_translate_lang="zh-tw"
        )
        interaction.response.send_message.assert_awaited_once()

        interaction.response.send_message.reset_mock()
        await cog.translate_status.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once()
        sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
        self.assertIn("zh-tw", str(sent_embed.fields[1].value))
        self.assertIn("<#20>", str(sent_embed.fields[2].value))

    def test_should_translate_for_japanese_tts(self):
        """非日本語判定 (ひらがな・カタカナ・www・辞書語・2文字漢字スキップ等) の検証"""
        from src.stt.hayamimi_client import should_translate_for_japanese_tts

        self.assertFalse(should_translate_for_japanese_tts("Apexやろう"))
        self.assertFalse(should_translate_for_japanese_tts("Hello 世界だよ"))
        self.assertFalse(should_translate_for_japanese_tts("www"))
        self.assertFalse(should_translate_for_japanese_tts("了解"))
        self.assertFalse(should_translate_for_japanese_tts("開始！"))
        self.assertFalse(should_translate_for_japanese_tts("Discord", [("discord", "ディスコード")]))

        self.assertTrue(should_translate_for_japanese_tts("Hello everyone!"))
        self.assertTrue(should_translate_for_japanese_tts("안녕하세요"))
        self.assertTrue(should_translate_for_japanese_tts("今天天气很好"))

    async def test_auto_join_pairs_translation_persistence_and_normalization(self):
        """auto_join_pairs のペア単位 translate_enabled / translate_lang 永続化・正規化検証"""
        import tempfile
        import os
        from src.database import Database, normalize_auto_join_pairs

        norm = normalize_auto_join_pairs([
            {"vc_id": "111", "text_id": "222"},
            {"vc_id": "333", "text_id": "444", "translate_enabled": True, "translate_lang": "ZH-TW"},
            {"vc_id": "555", "text_id": "666", "translate_enabled": True, "translate_lang": "bad"},
        ])
        self.assertEqual(len(norm), 3)
        self.assertFalse(norm[0]["translate_enabled"])
        self.assertEqual(norm[0]["translate_lang"], "en")
        self.assertTrue(norm[1]["translate_enabled"])
        self.assertEqual(norm[1]["translate_lang"], "zh-tw")
        self.assertEqual(norm[2]["translate_lang"], "en")

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_pairs.db")
            db = Database(db_path)
            await db.init_db()
            try:
                await db.update_guild_settings(
                    1000,
                    auto_join_pairs=[
                        {"vc_id": 101, "text_id": 201, "translate_enabled": False, "translate_lang": "en"},
                        {"vc_id": 102, "text_id": 202, "translate_enabled": True, "translate_lang": "ko"},
                    ],
                )
                db.invalidate_guild_cache(1000)
                loaded = await db.get_guild_settings(1000)
                pairs = loaded["auto_join_pairs"]
                self.assertEqual(len(pairs), 2)
                self.assertFalse(pairs[0]["translate_enabled"])
                self.assertTrue(pairs[1]["translate_enabled"])
                self.assertEqual(pairs[1]["translate_lang"], "ko")
            finally:
                await db.close()

    async def test_vc_handler_pair_translation_reads_foreign_as_japanese(self):
        """自動参加ペアの翻訳ONチャンネルで多言語→日本語訳読み上げ＆リプライ追記が動く検証"""
        import discord
        from src.cogs.vc_handler import VCHandlerCog

        cfg = AppConfig()
        cfg.hayamimi_ws_url = "ws://127.0.0.1:8833/ws/stream"
        mock_bot = MagicMock()
        mock_db = MagicMock()
        mock_cache = MagicMock()
        mock_tts = MagicMock()

        cog = VCHandlerCog(mock_bot, cfg, mock_db, mock_cache, mock_tts)
        try:
            state = cog.get_guild_state(100)
            state.bound_text_channel_id = 200
            state.is_playing = False
            mock_vc = MagicMock()
            mock_vc.is_connected.return_value = True
            mock_guild = MagicMock()
            mock_guild.id = 100
            mock_guild.name = "TestGuild"
            mock_guild.voice_client = mock_vc
            mock_guild.get_member.return_value = None

            guild_cfg = {
                "ignore_prefixes": [";"],
                "max_text_length": 100,
                "read_username": 0,
                "dedup_window_seconds": 5.0,
                "dedup_max_length": 10,
                "speculative_timeout_seconds": 1.5,
                "tts_translate_enabled": 0,  # サーバー全体は OFF
                "tts_translate_lang": "en",
                "auto_join_pairs": [
                    {"vc_id": 150, "text_id": 200, "translate_enabled": True, "translate_lang": "ko"},
                    {"vc_id": 151, "text_id": 201, "translate_enabled": False, "translate_lang": "en"},
                ],
            }
            mock_db.get_guild_settings = AsyncMock(return_value=guild_cfg)
            mock_db.get_dictionary_words = AsyncMock(return_value=[])
            mock_db.get_ng_words = AsyncMock(return_value=[])
            mock_db.get_or_create_user_settings = AsyncMock(return_value={
                "speaker_id": "nanami",
                "pitch": "0%",
                "speed": 1.0,
                "volume": 0.8,
            })

            def _make_msg(content: str, mid: int, ch_id: int = 200) -> MagicMock:
                m = MagicMock(spec=discord.Message)
                m.id = mid
                m.content = content
                m.guild = mock_guild
                m.channel = MagicMock(id=ch_id)
                m.author = MagicMock(id=555, bot=False, display_name="UserA")
                m.created_at = None
                m.reply = AsyncMock()
                return m

            def _spawn(coro, **kw):
                return asyncio.create_task(coro)

            with patch.object(cog, "_playback_loop", new=AsyncMock()), \
                 patch("src.cogs.vc_handler.safe_create_task", side_effect=_spawn):

                # 1. ペア翻訳 ON チャンネル (200) で英語投稿 -> 日本語に翻訳して読み上げ & [JA] リプライ
                cog.hayamimi_translator.translate = AsyncMock(return_value={
                    "ok": True,
                    "source_lang": "en",
                    "translations": {"ja": "こんにちは皆さん！"},
                })
                msg_en = _make_msg("Hello everyone!", 101, ch_id=200)
                await cog.on_message(msg_en)
                await asyncio.sleep(0.05)

                self.assertEqual(state.queue.qsize(), 1)
                item1 = await state.queue.get()
                self.assertEqual(item1.text, "こんにちは皆さん！")
                msg_en.reply.assert_awaited_once()
                self.assertEqual(msg_en.reply.call_args[0][0], "🌐 `[JA]` こんにちは皆さん！")

                # 2. ペア翻訳 ON チャンネル (200) で日本語投稿 -> 原文即時読み上げ & [KO] リプライ
                cog.hayamimi_translator.translate = AsyncMock(return_value={
                    "ok": True,
                    "source_lang": "ja",
                    "translations": {"ko": "수고하셨습니다."},
                })
                msg_ja = _make_msg("お疲れ様です", 102, ch_id=200)
                await cog.on_message(msg_ja)
                await asyncio.sleep(0.05)

                self.assertEqual(state.queue.qsize(), 1)
                item2 = await state.queue.get()
                self.assertEqual(item2.text, "お疲れ様です")
                msg_ja.reply.assert_awaited_once()
                self.assertEqual(msg_ja.reply.call_args[0][0], "🌐 `[KO]` 수고하셨습니다.")

                # 3. 翻訳 API 失敗時は原文ベース (TextCleaner整形後) のフォールバックで読み上げ継続
                cog.hayamimi_translator.translate = AsyncMock(return_value=None)
                msg_err = _make_msg("Good night!", 103, ch_id=200)
                await cog.on_message(msg_err)
                await asyncio.sleep(0.05)

                self.assertEqual(state.queue.qsize(), 1)
                item3 = await state.queue.get()
                self.assertEqual(item3.text, cog.text_cleaner.clean("Good night!"))
                msg_err.reply.assert_not_awaited()

                # 4. ペア翻訳 OFF チャンネル (201) では翻訳APIを呼ばず従来読み上げのまま
                state.bound_text_channel_id = 201
                cog.hayamimi_translator.translate = AsyncMock()
                msg_off = _make_msg("Good morning!", 104, ch_id=201)
                await cog.on_message(msg_off)
                await asyncio.sleep(0.05)

                self.assertEqual(state.queue.qsize(), 1)
                item4 = await state.queue.get()
                self.assertEqual(item4.text, cog.text_cleaner.clean("Good morning!"))
                cog.hayamimi_translator.translate.assert_not_awaited()
                msg_off.reply.assert_not_awaited()
        finally:
            await cog.hayamimi_translator.close()


if __name__ == "__main__":
    unittest.main()
