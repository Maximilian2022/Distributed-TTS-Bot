import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

import src.utils.text_cleaner as tc
from src.config import AppConfig
from src.utils.text_cleaner import TextCleaner
from src.web.server import WebServer


class TestEnglishKanaDictSystem(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._custom_path = Path(self._tmpdir.name) / "custom_english_kana.json"
        self._prev_env = os.environ.get("TTSBOT_CUSTOM_ENGLISH_DICT_PATH")
        os.environ["TTSBOT_CUSTOM_ENGLISH_DICT_PATH"] = str(self._custom_path)
        tc.ensure_english_kana_dicts_loaded(force=True)

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop("TTSBOT_CUSTOM_ENGLISH_DICT_PATH", None)
        else:
            os.environ["TTSBOT_CUSTOM_ENGLISH_DICT_PATH"] = self._prev_env
        tc.ensure_english_kana_dicts_loaded(force=True)
        self._tmpdir.cleanup()

    def test_system_dict_json_loaded_and_lookup_order(self):
        stats = TextCleaner.get_english_kana_stats()
        self.assertGreater(stats["system_count"], 10000)

        # SudachiDict 抽出辞書（system層）に入っている一般企業・サービス等の参照
        kana_amz, tier_amz = TextCleaner.resolve_english_word_kana("amazon", allow_e2k=False)
        self.assertEqual(kana_amz, "アマゾン")
        self.assertIn(tier_amz, ("builtin", "alkana", "system"))

        # builtin が alkana/system より優先されること
        kana_yt, tier_yt = TextCleaner.resolve_english_word_kana("YouTube", allow_e2k=False)
        self.assertEqual(kana_yt, "ユーチューブ")
        self.assertEqual(tier_yt, "builtin")

        # custom が最優先で上書きすること
        TextCleaner.upsert_custom_english_kana("YouTube", "ヨウツベ")
        kana_custom, tier_custom = TextCleaner.resolve_english_word_kana("YouTube", allow_e2k=False)
        self.assertEqual(kana_custom, "ヨウツベ")
        self.assertEqual(tier_custom, "custom")

        # 削除すれば builtin に戻ること
        self.assertTrue(TextCleaner.delete_custom_english_kana("youtube"))
        kana_reverted, tier_reverted = TextCleaner.resolve_english_word_kana("YouTube", allow_e2k=False)
        self.assertEqual(kana_reverted, "ユーチューブ")
        self.assertEqual(tier_reverted, "builtin")

    def test_www_slang_protected_from_english_and_e2k(self):
        cleaner = TextCleaner()
        cleaned = cleaner.clean("それは草wwwめっちゃwww")
        self.assertIn("わら", cleaned)
        self.assertNotIn("ダブリュー", cleaned)

        kana, tier = TextCleaner.resolve_english_word_kana("www")
        self.assertIsNone(kana)
        self.assertEqual(tier, "slang_skip")

    def test_custom_dict_persistence_batch_and_validation(self):
        # 不正入力拒否
        with self.assertRaises(ValueError):
            TextCleaner.upsert_custom_english_kana("1234", "イチニサンシ")
        with self.assertRaises(ValueError):
            TextCleaner.upsert_custom_english_kana("www", "ワラワラ")
        with self.assertRaises(ValueError):
            TextCleaner.upsert_custom_english_kana("validword", "")

        # 一括インポートと永続化
        count = TextCleaner.import_custom_english_kana_batch([
            ("OpenAI", "オープンエーアイ"),
            ("Kubernetes", "クバネティス"),
        ])
        self.assertEqual(count, 2)
        self.assertTrue(self._custom_path.exists())

        saved_json = json.loads(self._custom_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_json["entries"]["kubernetes"], "クバネティス")

        # forceリロード後も復元されること
        TextCleaner.reload_english_kana_dict()
        kana, tier = TextCleaner.resolve_english_word_kana("kubernetes", allow_e2k=False)
        self.assertEqual(kana, "クバネティス")
        self.assertEqual(tier, "custom")

        # 検索
        hits = TextCleaner.search_system_english_kana("kuber", limit=10)
        self.assertTrue(any(h["word"] == "kubernetes" and h["source"] == "custom" for h in hits))

    def test_e2k_fallback_cache_and_budget_guard(self):
        calls = []

        def fake_ngram(w):
            calls.append(("ngram", w))
            return w != "xyzab"

        fake_ngram.as_is = lambda w: "エックスワイゼットエービー"

        def fake_c2k(w):
            calls.append(("c2k", w))
            return f"カタ_{w}"

        with patch.object(tc, "_get_e2k_converters", return_value=(fake_ngram, fake_c2k)):
            # 初回推論 -> e2k 実行
            converted1, unknown1 = TextCleaner.convert_english_to_kana("QwertyZがXYZABと戦う")
            self.assertEqual(converted1, "カタ_qwertyzがエックスワイゼットエービーと戦う")
            self.assertEqual(unknown1, [])

            # 2回目は LRU キャッシュヒット (fake_c2k 再呼び出しなし)
            prev_calls = len(calls)
            converted2, unknown2 = TextCleaner.convert_english_to_kana("QwertyZがまた来た")
            self.assertEqual(converted2, "カタ_qwertyzがまた来た")
            self.assertEqual(unknown2, [])
            self.assertEqual(len(calls), prev_calls)

            # 1メッセージあたりの新規推論回数上限 (8語超過時は unknown_words へ退避)
            many_unknowns = " ".join(f"Unk{chr(65+i)}Word" for i in range(12))
            converted3, unknown3 = TextCleaner.convert_english_to_kana(many_unknowns)
            self.assertEqual(len(unknown3), 12 - tc._E2K_MAX_INFER_PER_TEXT)
            self.assertIn("UnkIWord", unknown3)
            self.assertIn("カタ_unkaword", converted3)

    def test_real_e2k_if_installed(self):
        ngram_fn, c2k_fn = tc._get_e2k_converters()
        if ngram_fn is None or c2k_fn is None:
            self.skipTest("e2k is not installed in current Python runtime")
        preview = TextCleaner.preview_english_kana("PydanticとYoutube")
        self.assertIn("ユーチューブ", preview["converted"])
        # Pydantic は未知語なので e2k / e2k_cache によってカタカナ化されるはず
        self.assertNotIn("Pydantic", preview["converted"])
        pyd_token = next(t for t in preview["tokens"] if t["word"] == "Pydantic")
        self.assertIn(pyd_token["tier"], ("e2k", "e2k_cache"))


class TestEnglishKanaWebAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._custom_path = Path(self._tmpdir.name) / "custom_english_kana.json"
        self._prev_env = os.environ.get("TTSBOT_CUSTOM_ENGLISH_DICT_PATH")
        os.environ["TTSBOT_CUSTOM_ENGLISH_DICT_PATH"] = str(self._custom_path)
        tc.ensure_english_kana_dicts_loaded(force=True)

        self.cache_mgr = MagicMock()
        self.cache_mgr.purge_by_word = AsyncMock(return_value=1)
        self.cache_mgr.purge_by_words = AsyncMock(return_value=2)
        self.server = WebServer(
            bot=MagicMock(),
            config=AppConfig(),
            database=MagicMock(),
            cache_manager=self.cache_mgr,
            tts_manager=MagicMock(),
        )
        self.test_server = TestServer(self.server.app, host="127.0.0.1")
        await self.test_server.start_server()
        self.client = TestClient(self.test_server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.test_server.close()
        if self._prev_env is None:
            os.environ.pop("TTSBOT_CUSTOM_ENGLISH_DICT_PATH", None)
        else:
            os.environ["TTSBOT_CUSTOM_ENGLISH_DICT_PATH"] = self._prev_env
        tc.ensure_english_kana_dicts_loaded(force=True)
        self._tmpdir.cleanup()

    async def test_english_dict_crud_preview_and_auth_guard(self):
        # 1. 未認証時は 401/403 で遮断
        unauth_res = await self.client.get("/api/english_dict")
        self.assertIn(unauth_res.status, (401, 403))

        # 2. 認証済み一般ユーザー: GET と preview は許可、POST/DELETE は 403
        with patch.object(self.server, "_require_auth", return_value={"user_id": 12345, "is_admin": False}), \
             patch.object(self.server, "_is_admin_user", new=AsyncMock(return_value=False)):
            get_res = await self.client.get("/api/english_dict?q=amazon")
            self.assertEqual(get_res.status, 200)
            body = await get_res.json()
            self.assertIn("stats", body)
            self.assertTrue(any(r["word"] == "amazon" for r in body["search_results"]))

            prev_res = await self.client.post("/api/english_dict/preview", json={"text": "Youtubeを見る"})
            self.assertEqual(prev_res.status, 200)
            prev_body = await prev_res.json()
            self.assertEqual(prev_body["converted"], "ユーチューブを見る")

            deny_res = await self.client.post("/api/english_dict", json={"word": "openai", "reading": "オープンエーアイ"})
            self.assertEqual(deny_res.status, 403)

        # 3. 管理者: 追加・一括・削除とキャッシュパージが機能すること
        with patch.object(self.server, "_require_auth", return_value={"user_id": 0, "is_admin": True}), \
             patch.object(self.server, "_is_admin_user", new=AsyncMock(return_value=True)):
            add_res = await self.client.post("/api/english_dict", json={"word": "OpenAI", "reading": "オープンエーアイ"})
            self.assertEqual(add_res.status, 200)
            add_body = await add_res.json()
            self.assertEqual(add_body["word"], "openai")
            self.cache_mgr.purge_by_word.assert_awaited_with("openai")

            bad_res = await self.client.post("/api/english_dict", json={"word": "bad_123", "reading": "ダメ"})
            self.assertEqual(bad_res.status, 400)

            batch_res = await self.client.post("/api/english_dict/import_batch", json={
                "items": [{"word": "Supabase", "reading": "スパベース"}]
            })
            self.assertEqual(batch_res.status, 200)
            batch_body = await batch_res.json()
            self.assertEqual(batch_body["imported_count"], 1)

            del_res = await self.client.delete("/api/english_dict/openai")
            self.assertEqual(del_res.status, 200)
            self.cache_mgr.purge_by_word.assert_awaited_with("openai")


if __name__ == "__main__":
    unittest.main()
