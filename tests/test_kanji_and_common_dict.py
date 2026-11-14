import asyncio
import io
import os
import sqlite3
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer

import src.utils.text_cleaner as tc
from src.cache_manager import CacheManager
from src.cogs.vc_handler import (
    MisreadSuggestionApprovalView,
    MisreadSuggestionEditModal,
    VCHandlerCog,
)
from src.config import AppConfig, VoiceConfig
from src.database import Database
from src.tts.manager import TTSManager
from src.utils.text_cleaner import TextCleaner
from src.web.server import WebServer


def _make_dummy_wav(frames: int = 240) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


class TestLocalTtsKanjiReading(unittest.TestCase):
    def test_safe_kanji_conversion_and_protections(self):
        # 漢字を含まないテキストはファストパスでそのまま返却
        plain = "こんにちは、DirectXとカタカナ123!"
        self.assertEqual(TextCleaner.convert_kanji_for_local_tts(plain), plain)

        # <<__BEEP:N__>> タグが破壊されないこと
        beep_text = "今日は<<__BEEP:1__>>で方角を見る"
        out_beep = TextCleaner.convert_kanji_for_local_tts(beep_text)
        self.assertIn("<<__BEEP:1__>>", out_beep)
        self.assertIn("で", out_beep)
        self.assertNotIn("方角", out_beep)

        # 数詞と後続の助数詞・接尾辞クラスタは元表記維持し、周囲の一般漢字は補完される
        out_num = TextCleaner.convert_kanji_for_local_tts("1月1日に2人で3本買った")
        self.assertIn("1月1日", out_num)
        self.assertIn("2人", out_num)
        self.assertIn("3本", out_num)
        self.assertIn("に", out_num)
        self.assertIn("で", out_num)

        out_day = TextCleaner.convert_kanji_for_local_tts("今日は1日だ")
        self.assertIn("1日だ", out_day)
        self.assertIn("は", out_day)

        # 助詞「は」「へ」「を」が読音化(わ/え/お)されず維持されること
        out_particle = TextCleaner.convert_kanji_for_local_tts("学校へ行き本を読む私はゲーマー")
        self.assertIn("へ", out_particle)
        self.assertIn("を", out_particle)
        self.assertIn("は", out_particle)

        # 文脈依存漢字・難読語の読み分け
        out_ik = TextCleaner.convert_kanji_for_local_tts("大会を行い会場へ行ってみよう")
        self.assertIn("おこない", out_ik)
        self.assertIn("いって", out_ik)

        out_hou = TextCleaner.convert_kanji_for_local_tts("方角とやり方を彼の方に聞く")
        self.assertIn("ほうがく", out_hou)
        self.assertIn("かた", out_hou)

        out_uma = TextCleaner.convert_kanji_for_local_tts("上手な人が上手いと言う")
        self.assertIn("じょうず", out_uma)
        self.assertIn("うまい", out_uma)

        out_hard = TextCleaner.convert_kanji_for_local_tts("薔薇が咲き刹那の見切りと生粋と篭もる")
        self.assertIn("ばら", out_hard)
        self.assertIn("せつな", out_hard)
        self.assertIn("きっすい", out_hard)
        self.assertIn("こもる", out_hard)


class TestCommonReadingDictAndMMO(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._custom_path = Path(self._tmpdir.name) / "custom_common_reading.json"
        self._prev_env = os.environ.get("TTSBOT_CUSTOM_COMMON_DICT_PATH")
        os.environ["TTSBOT_CUSTOM_COMMON_DICT_PATH"] = str(self._custom_path)
        tc.ensure_common_reading_dicts_loaded(force=True)

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop("TTSBOT_CUSTOM_COMMON_DICT_PATH", None)
        else:
            os.environ["TTSBOT_CUSTOM_COMMON_DICT_PATH"] = self._prev_env
        tc.ensure_common_reading_dicts_loaded(force=True)
        self._tmpdir.cleanup()

    def test_mmo_preset_words_and_acronyms(self):
        stats = TextCleaner.get_common_reading_stats()
        self.assertGreaterEqual(stats["system_count"], 30)

        cleaner = TextCleaner(max_length=200)
        # 共通日本語・MMOゲーム辞書（漢字・英数混在・記号語）
        cleaned = cleaner.clean("タダゲー厨の教えて君がPスキルと3D酔いとDDoS攻撃で宝珠を落とすおk")
        self.assertIn("ただげーちゅう", cleaned)
        self.assertIn("おしえてくん", cleaned)
        self.assertIn("ぴーすきる", cleaned)
        self.assertIn("すりーでぃーよい", cleaned)
        self.assertIn("でぃーどすこうげき", cleaned)
        self.assertIn("ほうじゅ", cleaned)
        self.assertIn("おっけー", cleaned)

        # MMO 英語略語拡充の変換
        for word, expected in [
            ("DirectX", "ダイレクトエックス"),
            ("wb", "ダブリュービー"),
            ("melee", "メレー"),
            ("dupe", "デュープ"),
            ("mob", "モブ"),
            ("debuff", "デバフ"),
            ("thx", "サンクス"),
        ]:
            kana, tier = TextCleaner.resolve_english_word_kana(word, allow_e2k=False)
            self.assertEqual(kana, expected, f"failed for {word}")
            self.assertEqual(tier, "builtin")

        # 境界ガード確認: wb が別単語の一部(www等)に誤爆しないこと
        out_www = cleaner.clean("これは草www wbした")
        self.assertIn("わら", out_www)
        self.assertIn("ダブリュービー", out_www)

    def test_guild_dictionary_priority_over_common_dict(self):
        # サーバー個別辞書が登録されている場合、共通辞書より常に優先される
        guild_dict = [("宝珠", "たからたま"), ("タダゲー厨", "カスタムチュウ")]
        res = TextCleaner.apply_dictionary("宝珠と廃神とタダゲー厨", guild_dict)
        self.assertIn("たからたま", res)
        self.assertIn("カスタムチュウ", res)
        # サーバー辞書にない語は共通辞書が使われる
        self.assertIn("はいしん", res)

    def test_custom_common_dict_crud_persistence_and_batch(self):
        with self.assertRaises(ValueError):
            TextCleaner.upsert_custom_common_reading("", "ヨミ")
        with self.assertRaises(ValueError):
            TextCleaner.upsert_custom_common_reading("あ" * 65, "長い")

        # システム辞書の上書き＆永続化＆削除で元に戻ること
        w, r = TextCleaner.upsert_custom_common_reading("宝珠", "カスタムホウジュ")
        self.assertEqual((w, r), ("宝珠", "カスタムホウジュ"))
        self.assertEqual(TextCleaner.apply_dictionary("宝珠を拾った"), "カスタムホウジュを拾った")

        # 再ロード後もカスタムJSONから復元されること
        reloaded = TextCleaner.reload_common_reading_dict()
        self.assertEqual(reloaded["custom_count"], 1)
        self.assertEqual(TextCleaner.apply_dictionary("宝珠を拾った"), "カスタムホウジュを拾った")

        self.assertTrue(TextCleaner.delete_custom_common_reading("宝珠"))
        self.assertEqual(TextCleaner.apply_dictionary("宝珠を拾った"), "ほうじゅを拾った")

        cnt = TextCleaner.import_custom_common_reading_batch([
            ("新ボス", "しんぼす"),
            ("裏ボス", "うらぼす"),
        ])
        self.assertEqual(cnt, 2)
        self.assertEqual(TextCleaner.apply_dictionary("裏ボス戦"), "うらぼす戦")


class TestTTSManagerLocalKanjiRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.config = AppConfig()
        self.config.voices["vv_zundamon"] = VoiceConfig(
            id="vv_zundamon",
            name="ずんだもん",
            gender="female",
            description="local",
            provider="voicevox",
            voice_name="ずんだもん",
            style_id=3,
        )
        self.config.voices["azure_nanami"] = VoiceConfig(
            id="azure_nanami",
            name="Nanami",
            gender="female",
            description="cloud",
            provider="azure",
            voice_name="ja-JP-NanamiNeural",
        )
        self.cache_mgr = CacheManager(
            cache_dir=os.path.join(self._tmpdir.name, "cloud"),
            voicevox_cache_dir=os.path.join(self._tmpdir.name, "local"),
        )
        self.manager = TTSManager(config=self.config, cache_manager=self.cache_mgr)

    async def asyncTearDown(self):
        self._tmpdir.cleanup()

    async def test_local_tts_only_kanji_conversion_and_cache_isolation(self):
        wav = _make_dummy_wav()
        src_text = "薔薇を見て行ってみよう"
        converted_expected = TextCleaner.convert_kanji_for_local_tts(src_text)
        self.assertNotEqual(converted_expected, src_text)

        # キャッシュキー解決: ローカル + ON のみ変換テキストになる
        self.assertEqual(
            self.manager.resolve_cache_text(src_text, "voicevox", local_tts_kanji_reading=True),
            converted_expected,
        )
        self.assertEqual(
            self.manager.resolve_cache_text(src_text, "voicevox", local_tts_kanji_reading=False),
            src_text,
        )
        self.assertEqual(
            self.manager.resolve_cache_text(src_text, "azure", local_tts_kanji_reading=True),
            src_text,
        )

        # 実際の合成コール先へ渡される文字列を検証
        local_calls = []
        cloud_calls = []

        async def fake_try_local(engine_name, client, pool, text, *args, **kwargs):
            local_calls.append((engine_name, text))
            return wav

        async def fake_azure_synth(text, voice_name, pitch="default", speed=1.0):
            cloud_calls.append(text)
            return wav

        self.manager._try_local_engine = AsyncMock(side_effect=fake_try_local)
        self.manager.azure_client.is_available = MagicMock(return_value=True)
        self.manager.azure_client.synthesize = AsyncMock(side_effect=fake_azure_synth)

        # 1) VOICEVOX + ON -> かな補完テキストが渡る & キャッシュメタのtextは元表記
        path_on = await self.manager.get_audio_path(
            user_id=123,
            text=src_text,
            speaker_id="vv_zundamon",
            pitch="default",
            speed=1.0,
            local_tts_kanji_reading=True,
        )
        self.assertTrue(os.path.exists(path_on))
        self.assertEqual(local_calls[-1], ("voicevox", converted_expected))

        # 2) VOICEVOX + OFF -> 別キャッシュになり原文が渡る
        path_off = await self.manager.get_audio_path(
            user_id=123,
            text=src_text,
            speaker_id="vv_zundamon",
            pitch="default",
            speed=1.0,
            local_tts_kanji_reading=False,
        )
        self.assertNotEqual(path_on, path_off)
        self.assertEqual(local_calls[-1], ("voicevox", src_text))

        # 3) Azure (クラウド) + ON -> 原文のまま渡る
        await self.manager.get_audio_path(
            user_id=123,
            text=src_text,
            speaker_id="azure_nanami",
            pitch="default",
            speed=1.0,
            local_tts_kanji_reading=True,
        )
        self.assertEqual(cloud_calls[-1], src_text)
        await asyncio.sleep(0.05)


class TestDatabaseKanjiAndSuggestions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.db"

    async def asyncTearDown(self):
        self._tmpdir.cleanup()

    async def test_guild_settings_migration_and_suggestion_override(self):
        # 正常スキーマを作成後、local_tts_kanji_reading のみ落として起動時ALTER TABLEマイグレーションを検証
        init_db = Database(str(self.db_path))
        await init_db.init_db()
        await init_db.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO guild_settings (guild_id, command_prefix) VALUES (1001, '!')")
        conn.execute("ALTER TABLE guild_settings DROP COLUMN local_tts_kanji_reading")
        conn.commit()
        conn.close()

        db = Database(str(self.db_path))
        await db.init_db()
        try:
            s = await db.get_guild_settings(1001)
            self.assertEqual(s.get("local_tts_kanji_reading"), 1)

            await db.update_guild_settings(1001, local_tts_kanji_reading=0)
            s_off = await db.get_guild_settings(1001)
            self.assertEqual(s_off.get("local_tts_kanji_reading"), 0)

            added = await db.add_ai_suggestion(1001, "秘奥義", "ヒオウギ", "難読漢字", source="reaction")
            self.assertTrue(added)
            row = await db.get_ai_suggestion_by_guild_word(1001, "秘奥義")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "pending")

            # 読みを上書き承認
            approved = await db.approve_ai_suggestion(row["id"], reading="ヒオウギワザ")
            self.assertIsNotNone(approved)
            self.assertEqual(approved["suggested_reading"], "ヒオウギワザ")
            self.assertEqual(approved["status"], "approved")

            guild_dict = dict(await db.get_dictionary_words(1001))
            self.assertEqual(guild_dict.get("秘奥義"), "ヒオウギワザ")
        finally:
            await db.close()


class TestCommonDictAndConfigWebAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._custom_path = Path(self._tmpdir.name) / "custom_common_reading.json"
        self._prev_env = os.environ.get("TTSBOT_CUSTOM_COMMON_DICT_PATH")
        os.environ["TTSBOT_CUSTOM_COMMON_DICT_PATH"] = str(self._custom_path)
        tc.ensure_common_reading_dicts_loaded(force=True)

        self.db_path = Path(self._tmpdir.name) / "web_test.db"
        self.db = Database(str(self.db_path))
        await self.db.init_db()

        self.cache_mgr = MagicMock()
        self.cache_mgr.purge_by_word = AsyncMock(return_value=2)
        self.cache_mgr.purge_by_words = AsyncMock(return_value=3)

        mock_guild = MagicMock()
        mock_guild.id = 2001
        mock_guild.name = "TestGuild"
        self.server = WebServer(
            bot=MagicMock(),
            config=AppConfig(),
            database=self.db,
            cache_manager=self.cache_mgr,
            tts_manager=MagicMock(),
        )
        self.server._resolve_guild = MagicMock(return_value=mock_guild)
        self.test_server = TestServer(self.server.app, host="127.0.0.1")
        await self.test_server.start_server()
        self.client = TestClient(self.test_server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.test_server.close()
        await self.db.close()
        if self._prev_env is None:
            os.environ.pop("TTSBOT_CUSTOM_COMMON_DICT_PATH", None)
        else:
            os.environ["TTSBOT_CUSTOM_COMMON_DICT_PATH"] = self._prev_env
        tc.ensure_common_reading_dicts_loaded(force=True)
        self._tmpdir.cleanup()

    async def test_common_dict_and_config_endpoints(self):
        # 未認証は 401/403
        unauth = await self.client.get("/api/common_dict")
        self.assertIn(unauth.status, (401, 403))

        # 認証済み非管理者: GET/preview は許可、変更系は 403
        with patch.object(self.server, "_require_auth", return_value={"user_id": 10, "is_admin": False}), \
             patch.object(self.server, "_is_admin_user", new=AsyncMock(return_value=False)):
            g = await self.client.get("/api/common_dict?q=宝珠")
            self.assertEqual(g.status, 200)
            gb = await g.json()
            self.assertTrue(any(x["word"] == "宝珠" for x in gb["search_results"]))

            p = await self.client.post("/api/common_dict/preview", json={"text": "宝珠とDirectXと薔薇"})
            self.assertEqual(p.status, 200)
            pb = await p.json()
            self.assertIn("ほうじゅ", pb["after_common_dict"])
            self.assertIn("ダイレクトエックス", pb["after_english_and_dict"])
            self.assertIn("ばら", pb["local_tts_kanji_converted"])

            deny = await self.client.post("/api/common_dict", json={"word": "邪気眼", "reading": "じゃきがん"})
            self.assertEqual(deny.status, 403)

        # 管理者: 追加・バッチ・クエリ削除・/api/config 保存
        with patch.object(self.server, "_require_auth", return_value={"user_id": 0, "is_admin": True}), \
             patch.object(self.server, "_is_admin_user", new=AsyncMock(return_value=True)), \
             patch.object(
                 self.server,
                 "_get_guild_and_perms",
                 new=AsyncMock(return_value=(MagicMock(id=2001), {"is_admin": True, "has_role": True})),
             ):
            add = await self.client.post("/api/common_dict", json={"word": "A/B", "reading": "えーびー"})
            self.assertEqual(add.status, 200)
            self.cache_mgr.purge_by_word.assert_awaited_with("A/B")

            batch = await self.client.post(
                "/api/common_dict/import_batch",
                json={"items": [{"word": "詠唱破棄", "reading": "えいしょうはき"}]},
            )
            self.assertEqual(batch.status, 200)
            self.assertEqual((await batch.json())["imported_count"], 1)

            # スラッシュ含有語を ?word= クエリで安全削除
            dele = await self.client.delete("/api/common_dict?word=A%2FB")
            self.assertEqual(dele.status, 200)
            self.cache_mgr.purge_by_word.assert_awaited_with("A/B")

            cfg_get = await self.client.get("/api/config?guild_id=2001")
            self.assertEqual(cfg_get.status, 200)
            self.assertTrue((await cfg_get.json())["local_tts_kanji_reading"])

            cfg_post = await self.client.post(
                "/api/config",
                json={"guild_id": 2001, "local_tts_kanji_reading": False},
            )
            self.assertEqual(cfg_post.status, 200)
            cfg_get2 = await self.client.get("/api/config?guild_id=2001")
            self.assertFalse((await cfg_get2.json())["local_tts_kanji_reading"])


class TestReactionMisreadApprovalUI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "reaction.db"
        self.db = Database(str(self.db_path))
        await self.db.init_db()

        self.cache_mgr = MagicMock()
        self.cache_mgr.purge_by_word = AsyncMock(return_value=4)
        self.bot = MagicMock()
        self.cog = VCHandlerCog(
            bot=self.bot,
            config=AppConfig(),
            database=self.db,
            cache_manager=self.cache_mgr,
            tts_manager=MagicMock(),
        )

    async def asyncTearDown(self):
        self.cog.cog_unload()
        await self.db.close()
        self._tmpdir.cleanup()

    async def test_learn_from_reaction_sends_reply_ui_and_debounces(self):
        self.cog.gemini_tutor.analyze_misread_text = AsyncMock(
            return_value=[{"word": "刹那", "reading": "セツナ", "reason": "難読語"}]
        )
        msg = MagicMock(spec=discord.Message)
        msg.id = 999001
        msg.content = "刹那の見切り"
        msg.add_reaction = AsyncMock()
        reply_msg = MagicMock(spec=discord.Message)
        reply_msg.edit = AsyncMock()
        msg.reply = AsyncMock(return_value=reply_msg)

        await self.cog._learn_from_reaction(3001, msg)
        msg.add_reaction.assert_awaited_once_with("💡")
        msg.reply.assert_awaited_once()
        kwargs = msg.reply.call_args.kwargs
        self.assertIsInstance(kwargs["view"], MisreadSuggestionApprovalView)
        self.assertEqual(kwargs["mention_author"], False)

        # 300s 以内の連打はデバウンスされ再送されないこと
        await self.cog._learn_from_reaction(3001, msg)
        msg.reply.assert_awaited_once()

    async def test_approval_edit_and_reject_callbacks(self):
        await self.db.add_ai_suggestion(3002, "九十九", "キュウジュウキュウ", "読点誤読", source="reaction")
        row = await self.db.get_ai_suggestion_by_guild_word(3002, "九十九")
        entries = [{
            "id": row["id"],
            "word": "九十九",
            "reading": "キュウジュウキュウ",
            "reason": "読点誤読",
            "status": "pending",
        }]
        view = MisreadSuggestionApprovalView(self.cog, guild_id=3002, entries=entries)
        view.message = MagicMock(spec=discord.Message)
        view.message.edit = AsyncMock()

        # EditModal で読みを修正して登録＋キャッシュパージ
        modal = MisreadSuggestionEditModal(
            cog=self.cog,
            guild_id=3002,
            entry=entries[0],
            approval_view=view,
        )
        modal.reading_input._value = "ツクモ"
        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild = MagicMock(spec=discord.Guild)
        interaction.guild_id = 3002
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.display_name = "Tester"
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        await modal.on_submit(interaction)
        self.cache_mgr.purge_by_word.assert_awaited_with("九十九")
        gdict = dict(await self.db.get_dictionary_words(3002))
        self.assertEqual(gdict.get("九十九"), "ツクモ")
        view.message.edit.assert_awaited()

        # 即承認と却下フロー
        await self.db.add_ai_suggestion(3002, "鬼灯", "ホオズキ", "難読漢字", source="reaction")
        row2 = await self.db.get_ai_suggestion_by_guild_word(3002, "鬼灯")
        view2 = MisreadSuggestionApprovalView(
            self.cog,
            guild_id=3002,
            entries=[{"id": row2["id"], "word": "鬼灯", "reading": "ホオズキ", "reason": "難読漢字", "status": "pending"}],
        )
        inter2 = MagicMock(spec=discord.Interaction)
        inter2.user = MagicMock(spec=discord.Member)
        inter2.user.display_name = "Approver"
        inter2.response = MagicMock()
        inter2.response.defer = AsyncMock()
        inter2.edit_original_response = AsyncMock()

        await view2._on_approve_all(inter2)
        self.cache_mgr.purge_by_word.assert_awaited_with("鬼灯")
        gdict2 = dict(await self.db.get_dictionary_words(3002))
        self.assertEqual(gdict2.get("鬼灯"), "ホオズキ")

        await self.db.add_ai_suggestion(3002, "不採用語", "フサイヨウゴ", "テスト", source="reaction")
        row3 = await self.db.get_ai_suggestion_by_guild_word(3002, "不採用語")
        view3 = MisreadSuggestionApprovalView(
            self.cog,
            guild_id=3002,
            entries=[{"id": row3["id"], "word": "不採用語", "reading": "フサイヨウゴ", "reason": "テスト", "status": "pending"}],
        )
        inter3 = MagicMock(spec=discord.Interaction)
        inter3.user = MagicMock(spec=discord.Member)
        inter3.user.display_name = "Rejecter"
        inter3.response = MagicMock()
        inter3.response.defer = AsyncMock()
        inter3.edit_original_response = AsyncMock()

        await view3._on_reject_all(inter3)
        row3_after = await self.db.get_ai_suggestion_by_guild_word(3002, "不採用語")
        self.assertEqual(row3_after["status"], "rejected")
