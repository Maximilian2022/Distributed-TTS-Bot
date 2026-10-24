import unittest
from src.utils.text_cleaner import TextCleaner

class TestTextCleaner(unittest.TestCase):
    def test_clean_basic_text(self):
        cleaner = TextCleaner()
        raw = "こんにちは、テストです！"
        cleaned = cleaner.clean(raw)
        self.assertIn("こんにちは", cleaned)
        self.assertIn("テストです", cleaned)

    def test_split_into_chunks_short_text(self):
        text = "短いテキストです。"
        chunks = TextCleaner.split_into_chunks(text)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertTrue(any("短いテキストです" in c.text for c in chunks))

    def test_split_into_chunks_hardcap_30(self):
        # 句読点のない60文字以上の長文が確実に30文字以内に分割されること
        long_text = "これは句読点が一切入っていないとても長い文章であり三十文字のハードキャップによって確実に安全に分割される必要があります"
        chunks = TextCleaner.split_into_chunks(long_text)
        self.assertGreaterEqual(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(len(c.text), 30, f"Chunk exceeded 30 chars: '{c.text}' ({len(c.text)} chars)")

    def test_slang_w_protection(self):
        # 英単語内の w / W が分割されたり破壊されないこと
        text = "Hello World and Windows."
        chunks = TextCleaner.split_into_chunks(text)
        combined = "".join(c.text for c in chunks)
        self.assertTrue("World" in combined or "world" in combined.lower())

    def test_author_prefix_separation(self):
        # 発言者名プレフィックスが第0パーツとして正しく抽出されること
        text = "テスト本文です。"
        prefix = "太郎さん、"
        chunks = TextCleaner.split_into_chunks(text, author_prefix=prefix)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0].text, prefix)
        self.assertTrue(getattr(chunks[0], "is_name_prefix", False))

if __name__ == "__main__":
    unittest.main()
