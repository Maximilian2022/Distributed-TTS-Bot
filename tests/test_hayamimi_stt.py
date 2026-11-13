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


if __name__ == "__main__":
    unittest.main()
