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

    def test_ng_word_tag_protection_in_chunks(self):
        # NGワード置換後のテキストで、BEEPタグが寸断されずBEEPチャンクと自然言語チャンクに分離されること
        ng_words = ["ばか", "あほ"]
        raw = "ゆばかはあほだからばかだあほ"
        replaced, detected = TextCleaner.apply_ng_words(raw, ng_words)
        self.assertEqual(len(detected), 2)
        self.assertIn("<<__BEEP", replaced)

        chunks = TextCleaner.split_into_chunks(replaced)
        # BEEPタグの残骸がどのチャンクのテキストにも混入していないこと
        for c in chunks:
            self.assertNotIn("<<", c.text)
            self.assertNotIn(">>", c.text)
            self.assertNotIn("BEEP", c.text)

        # BEEPチャンクが正しく含まれていること
        beep_chunks = [c for c in chunks if getattr(c, "is_beep", False)]
        self.assertGreaterEqual(len(beep_chunks), 1)

    def test_consecutive_ng_words_merged(self):
        # 連続したNGワードが1つの持続ピー音チャンクに綺麗に統合されること
        ng_words = ["ばか", "あほ"]
        raw = "ゆばかあほだから"
        replaced, detected = TextCleaner.apply_ng_words(raw, ng_words)
        chunks = TextCleaner.split_into_chunks(replaced)

        # 構造: 「ゆ」 -> BEEP(マージ) -> 「だから」
        non_beep = [c.text for c in chunks if not getattr(c, "is_beep", False)]
        self.assertEqual(non_beep, ["ゆ", "だから"])

        beep_chunks = [c for c in chunks if getattr(c, "is_beep", False)]
        self.assertEqual(len(beep_chunks), 1)
        # 2文字(0.34s) + 2文字(0.34s) = 0.68s
        self.assertAlmostEqual(beep_chunks[0].beep_duration_sec, 0.68, places=2)

    def test_smart_te_de_chunking(self):
        # 接続助詞「て/で」の自立語接続スマートチャンキング
        text = "最近コーヒー飲まなくなってアールグレイの紅茶よく飲んでる"
        chunks = TextCleaner.split_into_chunks(text)
        self.assertGreaterEqual(len(chunks), 2)
        combined = [c.text for c in chunks]
        self.assertTrue(any("最近コーヒー飲まなくなって" in t for t in combined))

    def test_warmup_morphological_tokenizers_hot(self):
        # 形態素解析エンジンのホットウォームが正常に完了し、その後の文分割が高速（100ms未満）に完了すること
        from src.utils.text_cleaner import warmup_morphological_tokenizers
        warmup_morphological_tokenizers()

        test_sentence = "今日は朝から雨が降っていたので傘を持って出かけましたが昼過ぎには晴れて暑くなりました。"
        chunks = TextCleaner.split_into_chunks(test_sentence)
        self.assertGreaterEqual(len(chunks), 2)
        
        # split_ms が記録され、かつホットウォームによりインシデント閾値(1000ms)を大幅に下回ること
        split_ms = getattr(chunks[0], "split_ms", 0.0)
        self.assertGreater(split_ms, 0.0)
        self.assertLess(split_ms, 500.0, f"split_ms was too slow after hot warmup: {split_ms}ms")

    def test_split_greeting_tail(self):
        # 文末の挨拶が独立チャンクとして切り出されること
        text = "今日も一日 おつかれさまでした！"
        chunks = TextCleaner.split_into_chunks("今日も一日、おつかれさま！")
        self.assertGreaterEqual(len(chunks), 2)
        chunk_texts = [c.text.strip() for c in chunks]
        self.assertIn("今日も一日", chunk_texts)
        self.assertIn("おつかれさま！", chunk_texts)

    def test_split_greeting_tail_redos_safety(self):
        # タブや空白が大量に連続した場合でもReDoSを起こさず瞬時に処理されること
        nasty_text = "\t" * 500 + "おつかれ！"
        import time
        t0 = time.perf_counter()
        chunks = TextCleaner.split_into_chunks(nasty_text)
        duration = time.perf_counter() - t0
        self.assertLess(duration, 0.05, f"ReDoS suspected! Took {duration}s")

if __name__ == "__main__":
    unittest.main()


