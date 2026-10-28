import re
import time
import emoji
import logging
import threading
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict
import discord
from src.utils.streaming_audio import calc_beep_duration
from src.utils.latency_profiler import latency_profiler

logger = logging.getLogger(__name__)

# SudachiPy 形態素解析器のシングルトン保持用 (C-3)
_sudachi_tokenizer = None
_sudachi_failed = False
_sudachi_split_mode = None
_sudachi_lock = threading.Lock()

# 高速化: 辞書置換および文分割用の正規表現キャッシュ & 事前コンパイル定数 (PERF-1, PERF-2)
_DICT_REGEX_CACHE: Dict[Tuple[str, str], re.Pattern] = {}
_SPLIT_CONJ_REGEX_CACHE: Dict[int, re.Pattern] = {}
_RE_ENGLISH_WORD = re.compile(r'\b[a-zA-Z]{2,}\b')
_RE_SPLIT_PUNCT = re.compile(r'([。！？]+[）\)\]」』】]*[\s]*)')
_RE_SPLIT_COMMA = re.compile(r'(、+)')
_RE_STRIP_PUNCT_SPACES = re.compile(r'[、。！？\s]')
_RE_KANJI_ONLY = re.compile(r'^[\u4e00-\u9faf]+$')
_RE_ALNUM_ONLY = re.compile(r'^[a-zA-Z0-9]+$')
_RE_HAS_READABLE = re.compile(r'[a-zA-Z0-9\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf]')

# テキスト前処理・スラング正規化用の事前コンパイル定数 (PERF)
_RE_DEMOJI_COLON = re.compile(r':([a-zA-Z0-9_\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uffef\u4e00-\u9faf]+):')
_RE_SLANG_W_REPEAT = re.compile(r'[wｗW]{2,}')
_RE_SLANG_W_SINGLE = re.compile(r'(^|\s|[。！？、])([wｗW])($|\s|[。！？、])')
_RE_SLANG_8_REPEAT = re.compile(r'[8８]{3,}')
_RE_SLANG_EXCL_REPEAT = re.compile(r'[!！]{2,}')
_RE_SLANG_QUES_REPEAT = re.compile(r'[\?？]{2,}')
_RE_SLANG_DASH_REPEAT = re.compile(r'[~〜ー]{2,}')
_RE_SLANG_KUSA_REPEAT = re.compile(r'草{2,}')
# 安全な末尾トリム用文字セット (ReDoS / CWE-1333 根本防止: rstrip による完全 O(N) 処理)
_TRAILING_MARKS_CHARS = "!！?？~〜ーwｗ \t\r\n\u3000。、"
_TRAILING_MARKS_NO_QUES_CHARS = "!！~〜ーwｗ \t\r\n\u3000。、"
_QUESTION_CHARS = "?？"
_RE_HAS_QUES = re.compile(r'[?？]')
_RE_BEEP_TAG = re.compile(r'<<(?:\u30d4\u30fc\u97f3|__BEEP)(?::(\d+))?(?:__)?>>')
_RE_BEEP_SPLIT = re.compile(r'(<<(?:\u30d4\u30fc\u97f3|__BEEP)(?::\d+)?(?:__)?>>)')

# 句読点なし多段チャンキング & 30文字ハードキャップ用の事前コンパイル定数 (PERF-3)
_MAX_CHUNK_CHARS = 30
_MIN_PART_CHARS = 5
_RE_SPLIT_SLANG_DELIM = re.compile(r'((?:(?<![a-zA-Z0-9])[wｗW]+(?![a-zA-Z0-9])|草+|パチパチ))[\s\u3000]*')
_RE_SPLIT_ENDING = re.compile(r'((?:です|ます|でした|ました|ですね|ですよ|ですよね|だよね|じゃん|だし)(?:よ|ね|な)?)(?=[^\s、。！？\d])')

# 定型挨拶・相槌の文頭/文末スライス用正規表現 (キャッシュ再利用率・ヒット率最大化)
_RE_SPLIT_GREETING_HEAD = re.compile(
    r'^(おはようございます|おはよう|こんにちは|こんばんわ|こんばんは|おつかれさまです|おつかれさま|おつかれ|おつ|ありがとうございます|ありがとう|よろしくお願いします|よろしく|了解です|了解|なるほど|草|まじかよ|まじか|助かる|はい|いいえ)'
    r'([！!？?wｗ\s、。]+|$)',
    re.IGNORECASE
)
_RE_SPLIT_GREETING_TAIL = re.compile(
    r'(おはようございます|おはよう|こんにちは|こんばんわ|こんばんは|おつかれさまです|おつかれさま|おつかれ|おつ|ありがとうございます|ありがとう|よろしくお願いします|よろしく|了解です|了解|なるほど|草|まじかよ|まじか|助かる)'
    r'([！!？?wｗ\s、。]*)$',
    re.IGNORECASE
)


# 文頭接続詞・相槌の先頭スライス用正規表現 (先頭即時ヒット・TTFA短縮)
_RE_SPLIT_HEAD_CONJ = re.compile(
    r'^(でも、|でも|だから、|だから|確かに、|確かに|あと、|あと|そういえば、|そういえば|いや、|いや|あ、|え、|えーと、|うーん、|うーん)'
    r'(?=[^\s、。！？\d])'
)

_janome_tokenizer = None
_janome_failed = False
_janome_lock = threading.Lock()

def get_sudachi_tokenizer():
    """SudachiPy 形態素解析器をスレッドセーフに遅延初期化して返却"""
    global _sudachi_tokenizer, _sudachi_failed, _sudachi_split_mode
    if _sudachi_tokenizer is not None:
        return _sudachi_tokenizer, _sudachi_split_mode
    if _sudachi_failed:
        return None, None
    with _sudachi_lock:
        if _sudachi_tokenizer is None and not _sudachi_failed:
            try:
                import sudachipy
                dict_obj = sudachipy.Dictionary()
                if hasattr(dict_obj, "tokenizer"):
                    _sudachi_tokenizer = dict_obj.tokenizer()
                else:
                    _sudachi_tokenizer = dict_obj.create()
                _sudachi_split_mode = sudachipy.SplitMode.C
                logger.info("✨ [TextCleaner] 高性能形態素解析エンジン SudachiPy (Mode C) をロードしました。")
            except Exception as e:
                _sudachi_failed = True
                logger.debug(f"SudachiPy unavailable, falling back to Janome/regex: {e}")
    return _sudachi_tokenizer, _sudachi_split_mode

def get_janome_tokenizer():
    """Janome 形態素解析器をスレッドセーフに遅延初期化して返却"""
    global _janome_tokenizer, _janome_failed
    if _janome_tokenizer is not None:
        return _janome_tokenizer
    if _janome_failed:
        return None
    with _janome_lock:
        if _janome_tokenizer is None and not _janome_failed:
            try:
                from janome.tokenizer import Tokenizer
                _janome_tokenizer = Tokenizer()
                logger.info("✨ [TextCleaner] Janome 形態素解析エンジンをロードしました (Fallback)。")
            except Exception as e:
                _janome_failed = True
                logger.debug(f"Janome tokenizer unavailable, falling back to regex: {e}")
    return _janome_tokenizer

def warmup_morphological_tokenizers():
    """
    形態素解析エンジン（SudachiPy / Janome）を起動時・バックグラウンドで完全事前ロード（ホットウォーム）し、
    最初のリクエストや語彙初回出現時の辞書展開・mmapページフォールト（1.5s〜7.4sの遅延）を根絶する。
    """
    t_start = time.perf_counter()
    warm_samples = [
        "私は昨日、東京駅で美味しいご飯を食べたのでとても満足しました。しかし、明日は大雨が降るそうです。",
        "ちゃんとdm受け取れるようにしとけやコラって書いてあるよ。ねえゆきくん閣下にもらったジェムの倍以上減ったから補填して！",
        "【気象警報】新潟地方に大雨警報が発表されました。低い土地の浸水や河川の増水に警戒してください。"
    ]

    # 1. SudachiPy のホットウォーム
    s_tok, s_mode = get_sudachi_tokenizer()
    if s_tok is not None and s_mode is not None:
        try:
            for sample in warm_samples:
                _ = list(s_tok.tokenize(sample, s_mode))
        except Exception as e:
            logger.debug(f"SudachiPy warm-up tokenize error: {e}")

    # 2. Janome のホットウォーム (フォールバック用)
    j_tok = get_janome_tokenizer()
    if j_tok is not None:
        try:
            for sample in warm_samples:
                _ = list(j_tok.tokenize(sample))
        except Exception as e:
            logger.debug(f"Janome warm-up tokenize error: {e}")

    # 3. TextCleaner の文章分割パイプライン全体のホットウォーム
    try:
        for sample in warm_samples:
            _ = TextCleaner.split_into_chunks(sample, author_prefix="テストユーザー")
    except Exception as e:
        logger.debug(f"TextCleaner warm-up error: {e}")

    elapsed_ms = (time.perf_counter() - t_start) * 1000.0
    logger.info(f"✨ [TextCleaner] 形態素解析エンジンのホットウォーム完了 (SudachiPy / Janome, 所要時間: {elapsed_ms:.1f}ms)")

@dataclass
class TextChunk:
    text: str
    pause_after_ms: int = 0
    split_ms: float = 0.0
    is_name_prefix: bool = False
    is_beep: bool = False
    beep_duration_sec: float = 0.4
    is_followed_by_beep: bool = False

class TextCleaner:
    def __init__(self, max_length: int = 50):
        self.max_length = max_length
        self._url_pattern = re.compile(r'https?://[^\s]+', re.IGNORECASE)
        self._user_mention_pattern = re.compile(r'<@!?(\d+)>')
        self._role_mention_pattern = re.compile(r'<@&(\d+)>')
        self._channel_mention_pattern = re.compile(r'<#(\d+)>')
        self._custom_emoji_pattern = re.compile(r'<a?:([a-zA-Z0-9_]+):\d+>')
        self._newline_pattern = re.compile(r'[\r\n]+')
        self._space_pattern = re.compile(r'\s+')
        self.last_detected_unknown_words: List[str] = []
        self.last_detected_ng_words: List[str] = []

    def should_ignore(self, message: discord.Message, ignore_prefixes: Optional[List[str]] = None) -> bool:
        """無視すべきメッセージ（Botの発言、コマンド行、除外プレフィックス等）かを判定"""
        # Bot自身の発言や他Botの発言を除外
        if message.author.bot:
            return True

        content = message.content.strip()
        if not content:
            return True

        # / や ! で始まるコマンド行は除外
        if content.startswith(('/', '!')):
            return True

        # 特定の文字から始まるテキストは読み上げを行わない
        if ignore_prefixes and content.startswith(tuple(ignore_prefixes)):
            return True

        return False

    @staticmethod
    def is_ignored_text(text: str, ignore_prefixes: Optional[List[str]] = None) -> bool:
        """テキスト単体に対して除外プレフィックスまたはコマンド判定を行う"""
        stripped = text.strip()
        if not stripped:
            return True
        if stripped.startswith(('/', '!')):
            return True
        if ignore_prefixes and stripped.startswith(tuple(ignore_prefixes)):
            return True
        return False

    def clean(
        self,
        raw_text: str,
        guild: Optional[discord.Guild] = None,
        dictionary_entries: Optional[List[Tuple[str, str]]] = None,
        ng_words: Optional[List[str]] = None,
        max_length: Optional[int] = None
    ) -> str:
        """
        要件に基づくテキストの前処理・サニタイズを実施
        """
        text = raw_text

        # 1. メンションの置換
        if guild:
            # ユーザーメンション: <@!ID> または <@ID>
            def _replace_user(match):
                user_id = int(match.group(1))
                member = guild.get_member(user_id)
                if member:
                    return f"@{member.display_name}さん "
                return "@誰か "

            text = self._user_mention_pattern.sub(_replace_user, text)

            # ロールメンション: <@&ID>
            def _replace_role(match):
                role_id = int(match.group(1))
                role = guild.get_role(role_id)
                if role:
                    return f"@{role.name} "
                return "@ロール "

            text = self._role_mention_pattern.sub(_replace_role, text)

            # チャンネルメンション: <#ID>
            def _replace_channel(match):
                channel_id = int(match.group(1))
                channel = guild.get_channel(channel_id)
                if channel:
                    return f"#{channel.name} "
                return "#チャンネル "

            text = self._channel_mention_pattern.sub(_replace_channel, text)
        else:
            text = self._user_mention_pattern.sub("@誰か ", text)
            text = self._role_mention_pattern.sub("@ロール ", text)
            text = self._channel_mention_pattern.sub("#チャンネル ", text)

        # 2. URL置換
        text = self._url_pattern.sub(" URL ", text)

        # 3. カスタム絵文字置換: <:name:id> や <a:name:id> -> name
        text = self._custom_emoji_pattern.sub(r' \1 ', text)

        # 4. Unicode絵文字置換 (emoji.demojizeの日本語読み活用)
        try:
            # :笑顔: のように変換される
            demojized = emoji.demojize(text, language='ja')
            # :emoji_name: のコロンを除去して前後に空白を入れる
            text = _RE_DEMOJI_COLON.sub(r' \1 ', demojized)
        except Exception as e:
            logger.debug(f"Demojize skipped: {e}")

        # 4.5. NGワード置換 (ピー音プレースホルダー <<BEEP>> への変換 & 最長一致 & 誤爆防止)
        if ng_words:
            text, detected_ng = self.apply_ng_words(text, ng_words)
            self.last_detected_ng_words = detected_ng
        else:
            self.last_detected_ng_words = []

        # 5. 単語辞書置換 (最長一致 & 誤爆防止ガード)
        if dictionary_entries:
            text = self.apply_dictionary(text, dictionary_entries)

        # 6. alkana による英単語即時カタカナ変換 & 未知語検知
        text, detected_unknown = self.convert_english_to_kana(text)
        self.last_detected_unknown_words = detected_unknown

        # 7. スラング・表記揺れの正規化 (辞書置換後に適用)
        text = self.normalize_slang(text)

        # 改行・連続空白の整理 (改行は文境界として句点に置換)
        text = self._newline_pattern.sub("。 ", text)
        text = self._space_pattern.sub(" ", text).strip()

        # 8. 文字数制限 (超過部分は切り捨てて「以下略」を付与)
        limit = max_length if max_length is not None else self.max_length
        if len(text) > limit:
            text = text[:limit] + " 以下略"

        return text

    def clean_with_metadata(
        self,
        raw_text: str,
        guild: Optional[discord.Guild] = None,
        dictionary_entries: Optional[List[Tuple[str, str]]] = None,
        ng_words: Optional[List[str]] = None,
        max_length: Optional[int] = None
    ) -> Tuple[str, List[str]]:
        """サニタイズされたテキストと、検出された未知語リストを返却"""
        cleaned = self.clean(
            raw_text=raw_text,
            guild=guild,
            dictionary_entries=dictionary_entries,
            ng_words=ng_words,
            max_length=max_length
        )
        return cleaned, list(self.last_detected_unknown_words)

    @classmethod
    def apply_ng_words(cls, text: str, ng_words: Optional[List[str]]) -> Tuple[str, List[str]]:
        """
        NGワード置換 (最長一致 & 誤爆防止ガード)
        NGワードを検知してピー音トークン ' <<BEEP>> ' に置換し、検出されたNGワード一覧を返却
        """
        if not ng_words:
            return text, []

        detected: List[str] = []
        sorted_words = sorted([w.strip() for w in ng_words if w.strip()], key=len, reverse=True)

        for w in sorted_words:
            if not w or w not in text:
                continue

            matched = False
            # 1. 漢字のみで構成された単語（例: 「表」）: 前後に漢字がない場合のみ置換
            if _RE_KANJI_ONLY.match(w):
                cache_key = ("ng_kanji", w)
                pat = _DICT_REGEX_CACHE.get(cache_key)
                if pat is None:
                    pat = re.compile(rf'(?<![\u4e00-\u9faf]){re.escape(w)}(?![\u4e00-\u9faf])')
                    _DICT_REGEX_CACHE[cache_key] = pat
                if pat.search(text):
                    text = pat.sub(f" <<__BEEP:{len(w)}__>> ", text)
                    matched = True
            # 2. 英数字のみで構成された単語（例: 「ng」）: 前後に英数字がない場合のみ置換
            elif _RE_ALNUM_ONLY.match(w):
                cache_key = ("ng_alnum", w.lower())
                pat = _DICT_REGEX_CACHE.get(cache_key)
                if pat is None:
                    pat = re.compile(rf'(?<![a-zA-Z0-9]){re.escape(w)}(?![a-zA-Z0-9])', re.IGNORECASE)
                    _DICT_REGEX_CACHE[cache_key] = pat
                if pat.search(text):
                    text = pat.sub(f" <<__BEEP:{len(w)}__>> ", text)
                    matched = True
            # 3. ひらがな・カタカナ・記号・混在語
            else:
                if w in text:
                    text = text.replace(w, f" <<__BEEP:{len(w)}__>> ")
                    matched = True

            if matched:
                detected.append(w)

        return text, detected

    @classmethod
    def apply_dictionary(cls, text: str, dictionary_entries: Optional[List[Tuple[str, str]]]) -> str:
        """
        単語辞書置換 (最長一致: Longest-Match First & 誤爆防止境界ガード)
        単語長の長いものから優先適用し、漢字や英数字が長い熟語の一部として
        部分誤爆（例: 「表」が「表現」や「発表」にマッチする現象）するのを防止。
        """
        if not dictionary_entries:
            return text

        # 単語長の降順でソート（最長一致）
        sorted_entries = sorted(dictionary_entries, key=lambda x: len(x[0]), reverse=True)

        for word, reading in sorted_entries:
            w = word.strip()
            if not w or w not in text:
                continue

            # 1. 漢字のみで構成された単語（例: 「表」）: 前後に漢字がない場合のみ置換
            if _RE_KANJI_ONLY.match(w):
                cache_key = ("kanji", w)
                pat = _DICT_REGEX_CACHE.get(cache_key)
                if pat is None:
                    pat = re.compile(rf'(?<![\u4e00-\u9faf]){re.escape(w)}(?![\u4e00-\u9faf])')
                    _DICT_REGEX_CACHE[cache_key] = pat
                text = pat.sub(reading, text)
            # 2. 英数字のみで構成された単語（例: 「in」）: 前後に英数字がない場合のみ置換
            elif _RE_ALNUM_ONLY.match(w):
                cache_key = ("alnum", w.lower())
                pat = _DICT_REGEX_CACHE.get(cache_key)
                if pat is None:
                    pat = re.compile(rf'(?<![a-zA-Z0-9]){re.escape(w)}(?![a-zA-Z0-9])', re.IGNORECASE)
                    _DICT_REGEX_CACHE[cache_key] = pat
                text = pat.sub(reading, text)
            # 3. ひらがな・カタカナ・記号・混在語
            else:
                text = text.replace(w, reading)

        return text

    COMMON_ACRONYMS = {
        "vc": "ブイシー",
        "dm": "ディーエム",
        "url": "ユーアールエル",
        "pc": "ピーシー",
        "id": "アイディー",
        "ok": "オーケー",
        "ng": "エヌジー",
        "fps": "エフピーエス",
        "rpg": "アールピージー",
        "bot": "ボット",
        "ai": "エーアイ",
        "ui": "ユーアイ",
        "web": "ウェブ",
        "cpu": "シーピーユー",
        "gpu": "ジーピーユー",
        "ram": "ラム",
        "rom": "ロム",
        "ps": "プレイステーション",
        "switch": "スイッチ",
        "steam": "スチーム",
        "discord": "ディスコード",
        "twitter": "ツイッター",
        "youtube": "ユーチューブ",
        "twitch": "ツイッチ",
    }

    @classmethod
    def convert_english_to_kana(cls, text: str) -> Tuple[str, List[str]]:
        """
        英単語を alkana および事前略語マップを用いて即座にカタカナ化する。
        alkanaに未登録の未知の英字単語（造語・固有名詞等）を検出してリストとして返す。
        """
        unknown_words: List[str] = []

        try:
            import alkana
        except ImportError:
            alkana = None

        def _replace_word(m: re.Match) -> str:
            raw_w = m.group(0)
            lower_w = raw_w.lower()

            # 1. 事前略語マップ
            if lower_w in cls.COMMON_ACRONYMS:
                return cls.COMMON_ACRONYMS[lower_w]

            # 2. alkana による英単語即時変換
            if alkana is not None:
                kana = alkana.get_kana(lower_w)
                if kana:
                    return kana

            # 3. 未知語（2文字以上の未知英単語を抽出）
            if len(raw_w) >= 2 and raw_w not in unknown_words:
                unknown_words.append(raw_w)
            return raw_w

        # 英単語境界にマッチ（英字2文字以上・事前コンパイル定数を使用）
        converted_text = _RE_ENGLISH_WORD.sub(_replace_word, text)
        return converted_text, unknown_words

    @staticmethod
    def normalize_slang(text: str) -> str:
        """ネットスラングや記号の重複を正規化してキャッシュヒット率を向上 (PERF)"""
        # 連続する w / ｗ (2個以上) -> わら
        text = _RE_SLANG_W_REPEAT.sub('わら', text)
        # 文末または単独の w / ｗ -> わら
        text = _RE_SLANG_W_SINGLE.sub(r'\1わら\3', text)
        # 8888 などの拍手 -> パチパチ
        text = _RE_SLANG_8_REPEAT.sub('パチパチ', text)
        # 連続する感嘆符・疑問符の集約
        text = _RE_SLANG_EXCL_REPEAT.sub('！', text)
        text = _RE_SLANG_QUES_REPEAT.sub('？', text)
        # 重複する長音符 (〜〜, ーー)
        text = _RE_SLANG_DASH_REPEAT.sub('ー', text)
        # 連続する草 -> 草
        text = _RE_SLANG_KUSA_REPEAT.sub('草', text)
        return text

    @staticmethod
    def _split_long_sentence_morphological(
        sentence: str,
        delim: str,
        min_chars_threshold: int = 18,
        min_head_chars: int = 7,
        min_tail_chars: int = 5
    ) -> List[Tuple[str, str]]:
        """
        読点のない長い文（閾値以上、通常18文字以上、動的短文時は10〜14文字）を、
        超高速・高精度な形態素解析（SudachiPy）を用いて接続詞・接続助詞の自然な境界で分割する。
        SudachiPy が未導入または失敗した場合は Janome / 正規表現ルールに安全フォールバック。
        """
        raw_text = sentence.strip()
        if len(raw_text) < min_chars_threshold:
            return [(sentence, delim)]

        # 1. SudachiPy 形態素解析器の取得を試みる (最優先: Rust製超高速・最新辞書)
        tokenizer, split_mode = get_sudachi_tokenizer()

        # SudachiPy による文節分割
        if tokenizer is not None and split_mode is not None:
            try:
                tokens = list(tokenizer.tokenize(raw_text, split_mode))
                split_indices = []
                cur_len = 0
                for i, tok in enumerate(tokens):
                    surface = tok.surface()
                    cur_len += len(surface)
                    pos = tok.part_of_speech()
                    # 接続助詞の直後で分割 (前がmin_head_chars以上、後がmin_tail_chars以上残っていること)
                    if pos[0] == '助詞' and pos[1] == '接続助詞':
                        is_split_target = False
                        if surface in {'ので', 'から', 'けど', 'けれど', 'たら', 'なら', 'が'}:
                            is_split_target = True
                        elif surface in {'て', 'で'}:
                            # 「て/で」の接続: 直後が補助動詞（非自立動詞: いる、みる、おく、しまう等）や助詞（てから、ては等）は除外
                            # 自立語（名詞・副詞・形容詞・一般動詞など）が後続する場合のみ自然な文節境界として分割
                            if i + 1 < len(tokens):
                                next_tok = tokens[i + 1]
                                n_pos = next_tok.part_of_speech()
                                is_aux = (n_pos[0] == '動詞' and any('非自立' in p for p in n_pos)) or (n_pos[0] == '助詞')
                                if not is_aux:
                                    is_split_target = True
                        if is_split_target:
                            remaining_chars = len(raw_text) - cur_len
                            if cur_len >= min_head_chars and remaining_chars >= min_tail_chars:
                                split_indices.append((cur_len, 'conj_particle'))
                    # 接続詞の直前で分割
                    elif pos[0] == '接続詞' and surface in {'しかし', 'そして', 'また', 'だから', 'なお', 'ただ', 'あるいは', 'それに'}:
                        split_pos = cur_len - len(surface)
                        remaining_chars = len(raw_text) - split_pos
                        if split_pos >= min_head_chars and remaining_chars >= min_tail_chars:
                            split_indices.append((split_pos, 'conjunction'))

                if split_indices:
                    pieces: List[Tuple[str, str]] = []
                    last_pos = 0
                    for s_pos, _ in split_indices:
                        if (s_pos - last_pos) >= min_head_chars and (len(raw_text) - s_pos) >= min_tail_chars:
                            sub_str = raw_text[last_pos:s_pos].strip()
                            if sub_str:
                                pieces.append((sub_str, 'conj'))
                            last_pos = s_pos
                    if last_pos < len(raw_text):
                        rest = raw_text[last_pos:].strip()
                        if rest:
                            pieces.append((rest, delim))
                    if len(pieces) > 1:
                        return pieces
            except Exception as e:
                logger.debug(f"Sudachi tokenization error: {e}")

        # 2. Janome へのフォールバック
        tokenizer = get_janome_tokenizer()
        pieces: List[Tuple[str, str]] = []

        if tokenizer is not None:
            try:
                tokens = list(tokenizer.tokenize(raw_text))
                # 接続助詞（ので、から、けど、けれど、たら、なら、が、自立接続のて/で）または接続詞を探す
                split_indices = []
                cur_len = 0
                for i, tok in enumerate(tokens):
                    cur_len += len(tok.surface)
                    pos = tok.part_of_speech.split(',')
                    # 接続助詞の直後で分割
                    if pos[0] == '助詞' and pos[1] == '接続助詞':
                        is_split_target = False
                        if tok.surface in {'ので', 'から', 'けど', 'けれど', 'たら', 'なら', 'が'}:
                            is_split_target = True
                        elif tok.surface in {'て', 'で'}:
                            if i + 1 < len(tokens):
                                next_tok = tokens[i + 1]
                                n_pos = next_tok.part_of_speech.split(',')
                                is_aux = (n_pos[0] == '動詞' and any('非自立' in p for p in n_pos)) or (n_pos[0] == '助詞')
                                if not is_aux:
                                    is_split_target = True
                        if is_split_target:
                            remaining_chars = len(raw_text) - cur_len
                            if cur_len >= min_head_chars and remaining_chars >= min_tail_chars:
                                split_indices.append((cur_len, 'conj_particle'))
                    # 接続詞の直前で分割
                    elif pos[0] == '接続詞' and tok.surface in {'しかし', 'そして', 'また', 'だから', 'なお', 'ただ', 'あるいは'}:
                        split_pos = cur_len - len(tok.surface)
                        remaining_chars = len(raw_text) - split_pos
                        if split_pos >= min_head_chars and remaining_chars >= min_tail_chars:
                            split_indices.append((split_pos, 'conjunction'))

                if split_indices:
                    last_pos = 0
                    for s_pos, s_type in split_indices:
                        if (s_pos - last_pos) >= min_head_chars and (len(raw_text) - s_pos) >= min_tail_chars:
                            sub_str = raw_text[last_pos:s_pos].strip()
                            if sub_str:
                                pieces.append((sub_str, 'conj'))
                            last_pos = s_pos
                    if last_pos < len(raw_text):
                        rest = raw_text[last_pos:].strip()
                        if rest:
                            pieces.append((rest, delim))
                    if len(pieces) > 1:
                        return pieces
            except Exception as e:
                logger.debug(f"Morphological tokenization error: {e}")

        # 3. フォールバック: 接続助詞・接続詞の正規表現ルール分割 (事前コンパイルキャッシュ使用)
        min_p_match = max(3, min_tail_chars)
        compiled_conj = _SPLIT_CONJ_REGEX_CACHE.get(min_p_match)
        if compiled_conj is None:
            compiled_conj = re.compile(rf'(ので|から|けど|けれど|たら|なら)(?=[^、。！？\s]{{{min_p_match},}})')
            _SPLIT_CONJ_REGEX_CACHE[min_p_match] = compiled_conj
        parts = compiled_conj.split(raw_text)
        if len(parts) > 1:
            temp = []
            buf = ""
            for idx, p in enumerate(parts):
                buf += p
                if idx % 2 == 1: # マッチした助詞
                    if len(buf) >= min_head_chars and len(raw_text) - len("".join(parts[:idx+1])) >= min_tail_chars:
                        temp.append((buf.strip(), 'conj'))
                        buf = ""
            if buf:
                temp.append((buf.strip(), delim))
            if len(temp) > 1:
                return temp

        return [(sentence, delim)]

    @staticmethod
    def _split_by_slang_boundary(text: str, delim: str = "") -> List[Tuple[str, str]]:
        """スラング・笑い表現（w, 草, パチパチ）の直後でパーツ分割"""
        raw_len = len(_RE_STRIP_PUNCT_SPACES.sub('', text))
        if raw_len <= 14:
            return [(text, delim)]

        parts = _RE_SPLIT_SLANG_DELIM.split(text)
        if len(parts) <= 1:
            return [(text, delim)]

        chunks: List[Tuple[str, str]] = []
        for i in range(0, len(parts), 2):
            p = parts[i]
            d = parts[i + 1] if i + 1 < len(parts) else ""
            full = (p + d).strip()
            if full:
                chunks.append((full, "slang" if (i + 1 < len(parts)) else delim))

        # 最低文字数ガード (短すぎるパーツはマージ)
        merged: List[Tuple[str, str]] = []
        for c_text, c_delim in chunks:
            if merged and len(_RE_STRIP_PUNCT_SPACES.sub('', c_text)) < _MIN_PART_CHARS:
                prev_text, prev_delim = merged.pop()
                merged.append((prev_text + " " + c_text, c_delim))
            else:
                merged.append((c_text, c_delim))
        return merged if len(merged) > 1 else [(text, delim)]

    @staticmethod
    def _hardcap_slice(text: str, delim: str = "", max_chars: int = _MAX_CHUNK_CHARS) -> List[Tuple[str, str]]:
        """指定文字数（通常30文字、動的設定時は10〜14文字）を超える塊を、形態素自立語境界または強制スライスで安全分割"""
        effective_max = max(_MIN_PART_CHARS * 2, max_chars)
        raw_len = len(_RE_STRIP_PUNCT_SPACES.sub('', text))
        if raw_len <= effective_max:
            return [(text, delim)]

        tokenizer, split_mode = get_sudachi_tokenizer()
        slices: List[Tuple[str, str]] = []

        if tokenizer is not None and split_mode is not None:
            try:
                tokens = list(tokenizer.tokenize(text, split_mode))
                cur_slice = ""
                for tok in tokens:
                    s = tok.surface()
                    if len(cur_slice) + len(s) > effective_max and len(cur_slice) >= _MIN_PART_CHARS:
                        slices.append((cur_slice.strip(), "hardcap"))
                        cur_slice = s
                    else:
                        cur_slice += s
                if cur_slice.strip():
                    slices.append((cur_slice.strip(), delim))
                if len(slices) > 1:
                    return slices
            except Exception as e:
                logger.debug(f"Sudachi hardcap tokenization error: {e}")

        # フォールバック: 強制ハードスライス
        cur = 0
        while cur < len(text):
            end = min(cur + effective_max, len(text))
            part = text[cur:end].strip()
            if part:
                slices.append((part, "hardcap" if end < len(text) else delim))
            cur = end
        return slices if slices else [(text, delim)]

    @staticmethod
    def _hardcap_slice_30(text: str, delim: str = "") -> List[Tuple[str, str]]:
        """30文字ハードキャップ (後方互換ラッパー)"""
        return TextCleaner._hardcap_slice(text, delim, max_chars=_MAX_CHUNK_CHARS)

    @staticmethod
    def _split_by_space_boundary(sentence: str) -> List[str]:
        """
        英単語間の空白 (Hello World 等) を保護しつつ、空白境界で文を分割する。
        正規表現のバックトラッキング (ReDoS / CWE-1333) を排除した完全 O(N) 線形スキャン。
        """
        if not sentence:
            return []
        parts: List[str] = []
        current: List[str] = []
        i = 0
        n = len(sentence)
        while i < n:
            ch = sentence[i]
            if ch in (' ', '\t', '\n', '\r', '\u3000'):
                prev_alnum = (i > 0 and sentence[i - 1].isalnum() and sentence[i - 1].isascii())
                j = i
                while j < n and sentence[j] in (' ', '\t', '\n', '\r', '\u3000'):
                    j += 1
                next_alnum = (j < n and sentence[j].isalnum() and sentence[j].isascii())

                if prev_alnum and next_alnum:
                    current.append(sentence[i:j])
                else:
                    if current:
                        parts.append("".join(current))
                        current = []
                i = j
            else:
                current.append(ch)
                i += 1
        if current:
            parts.append("".join(current))
        return parts

    @staticmethod
    def _split_by_greetings_boundary(sentence: str, delim: str) -> List[Tuple[str, str]]:
        """
        文頭または文末に独立した定型挨拶・相槌・了解等のフレーズが存在する場合、
        前後の文脈から独立したチャンクとして切り離し、キャッシュヒット率を最大化する。
        """
        s = sentence.strip()
        if not s:
            return []

        # 1. 文頭チェック
        m_head = _RE_SPLIT_GREETING_HEAD.match(s)
        if m_head:
            matched_greeting = m_head.group(1)
            punct = m_head.group(2) or ""
            remainder = s[m_head.end():].strip()
            # 残りの文が実質2文字以上あればスライス採用
            if len(_RE_STRIP_PUNCT_SPACES.sub('', remainder)) >= 2:
                greeting_part = (matched_greeting + punct).strip()
                return [(greeting_part, "greeting"), (remainder, delim)]

        # 2. 文末チェック
        m_tail = _RE_SPLIT_GREETING_TAIL.search(s)
        if m_tail and m_tail.start() > 0:
            matched_greeting = m_tail.group(1)
            punct = m_tail.group(2) or ""
            prefix = s[:m_tail.start()].rstrip("、 \t\u3000").strip()
            # 前半の文が実質2文字以上あればスライス採用
            if len(_RE_STRIP_PUNCT_SPACES.sub('', prefix)) >= 2:
                greeting_part = (matched_greeting + punct).strip()
                return [(prefix, "greeting_before"), (greeting_part, delim or "greeting")]

        return [(sentence, delim)]

    @staticmethod
    def _split_by_head_conjunction(sentence: str, delim: str) -> List[Tuple[str, str]]:
        """
        先頭チャンクの文頭に「でも、」「だから」「確かに」等の接続詞・相槌がある場合、
        先頭独立チャンクとして切り出し、Time to First Audio (TTFA) のゼロ遅延化を図る。
        """
        s = sentence.strip()
        if not s:
            return []
        m = _RE_SPLIT_HEAD_CONJ.match(s)
        if m:
            conj = m.group(1)
            remainder = s[m.end():].lstrip(" 、,\t\u3000")
            if len(_RE_STRIP_PUNCT_SPACES.sub('', remainder)) >= 3:
                return [(conj, "head_conj"), (remainder, delim)]
        return [(sentence, delim)]

    @staticmethod
    def split_into_chunks(
        text: str,
        author_prefix: Optional[str] = None,
        engine: Optional[str] = None,
        speaker_id: Optional[str] = None,
        max_first_chunk_chars: Optional[int] = None,
        max_chunk_chars: Optional[int] = None,
    ) -> List['TextChunk']:
        """
        テキストを発言者名プレフィックスおよび多段文境界（句点・読点・スペース・スラング・終助詞・形態素・動的ハードキャップ）で超低遅延パーツ分割する。
        各パーツには音響ストリーミングに最適な間（pause_after_ms）を付与。
        各マシン・各エンジンの実測推論性能統計 (latency_profiler) に基づき、第1チャンク上限 (max_first_chunk_chars) を自律動的決定。
        """
        t_start = time.time()
        chunks: List[TextChunk] = []

        # 0. 動的第1チャンク文字数上限 (max_first_chunk_chars) の自律解決 (各マシン×各エンジン特性・クラウド回線速度を反映)
        effective_first_chars = max_first_chunk_chars

        resolved_engine = engine
        if not resolved_engine and speaker_id:
            spk = str(speaker_id).strip().lower()
            if "ci_" in spk or "coeiroink" in spk:
                resolved_engine = "coeiroink"
            elif "vv_" in spk or "voicevox" in spk:
                resolved_engine = "voicevox"
            elif "sv_" in spk or "sharevox" in spk:
                resolved_engine = "sharevox"
            elif "ojt_" in spk or "openjtalk" in spk:
                resolved_engine = "openjtalk"
            elif "az_" in spk or "azure" in spk:
                resolved_engine = "azure"
            elif "g_" in spk or "google" in spk:
                resolved_engine = "google"
            elif "edge" in spk:
                resolved_engine = "edge"

        is_cloud_engine = bool(resolved_engine and resolved_engine.lower() in {"edge", "azure", "google"})
        system_hardcap = 38 if is_cloud_engine else _MAX_CHUNK_CHARS
        effective_max_chars = max_chunk_chars or system_hardcap

        if effective_first_chars is None and resolved_engine:
            try:
                effective_first_chars = latency_profiler.get_dynamic_max_chars(
                    engine=resolved_engine,
                    target_budget_ms=1750.0,
                    min_chars=8,
                    max_chars=effective_max_chars
                )
                # 第2チャンク以降の上限 (effective_max_chars) も未指定時はエンジンの実測性能に連動スケーリング
                if max_chunk_chars is None and effective_first_chars is not None:
                    effective_max_chars = min(
                        system_hardcap,
                        max(effective_first_chars + 4, int(round(effective_first_chars * 1.5)))
                    )
            except Exception as e:
                logger.debug(f"Dynamic max chars resolution error: {e}")

        # 1. 発言者名プレフィックスがあれば先頭パーツとして追加 (間: 80ms)
        # NGワードピー音プレースホルダー (<<__BEEP...>> / <<ピー音...>>) を含む場合は前後でチャンク分割
        if author_prefix:
            prefix = author_prefix.strip()
            if prefix:
                if '<<__BEEP' in prefix or '<<ピー音' in prefix:
                    parts = _RE_BEEP_SPLIT.split(prefix)
                    for p_idx, p in enumerate(parts):
                        p_clean = p.strip()
                        if not p_clean:
                            continue
                        m = _RE_BEEP_TAG.match(p_clean)
                        if m:
                            char_count = int(m.group(1)) if m.group(1) else 3
                            beep_sec = calc_beep_duration(char_count)
                            chunks.append(TextChunk(text="", pause_after_ms=40, is_beep=True, beep_duration_sec=beep_sec, is_name_prefix=True))
                        else:
                            if _RE_HAS_READABLE.search(p_clean):
                                is_before_beep = (p_idx + 1 < len(parts) and bool(_RE_BEEP_TAG.match(parts[p_idx + 1].strip())))
                                sub_pause = 20 if is_before_beep else 80
                                chunks.append(TextChunk(text=p_clean, pause_after_ms=sub_pause, is_name_prefix=True, is_followed_by_beep=is_before_beep))
                else:
                    chunks.append(TextChunk(text=prefix, pause_after_ms=80, is_name_prefix=True))

        if not text or not text.strip():
            split_ms = round((time.time() - t_start) * 1000, 2)
            for c in chunks:
                c.split_ms = split_ms
            return chunks

        # 2. 本文の処理: NGワードピー音プレースホルダー (<<__BEEP...>> / <<ピー音...>>) の先行抽出
        is_first_plain_seg = True
        if '<<__BEEP' in text or '<<ピー音' in text:
            raw_segments = _RE_BEEP_SPLIT.split(text)
            for s_idx, seg in enumerate(raw_segments):
                clean_s = seg.strip()
                if not clean_s:
                    continue

                m = _RE_BEEP_TAG.match(clean_s)
                if m:
                    char_count = int(m.group(1)) if m.group(1) else 3
                    beep_sec = calc_beep_duration(char_count)
                    # 連続するNGワードは1つの持続ピー音にマージして自然に発話
                    if chunks and chunks[-1].is_beep:
                        chunks[-1].beep_duration_sec = min(2.0, chunks[-1].beep_duration_sec + beep_sec)
                    else:
                        chunks.append(TextChunk(text="", pause_after_ms=40, is_beep=True, beep_duration_sec=beep_sec))
                else:
                    # 次の非空セグメントがBEEPタグかどうかを先読み
                    next_is_beep = False
                    for peek_idx in range(s_idx + 1, len(raw_segments)):
                        peek_s = raw_segments[peek_idx].strip()
                        if peek_s:
                            next_is_beep = bool(_RE_BEEP_TAG.match(peek_s))
                            break
                    plain_chunks = TextCleaner._split_plain_text(
                        clean_s,
                        next_is_beep=next_is_beep,
                        max_first_chunk_chars=effective_first_chars if is_first_plain_seg else None,
                        max_chunk_chars=effective_max_chars,
                        is_first_plain_segment=is_first_plain_seg
                    )
                    chunks.extend(plain_chunks)
                    if plain_chunks:
                        is_first_plain_seg = False
        else:
            chunks.extend(TextCleaner._split_plain_text(
                text,
                next_is_beep=False,
                max_first_chunk_chars=effective_first_chars,
                max_chunk_chars=effective_max_chars,
                is_first_plain_segment=True
            ))

        # 分割結果が空なら全文を1パーツとして扱う
        if not chunks and text.strip():
            if '<<__BEEP' in text or '<<ピー音' in text:
                chunks.append(TextChunk(text="", pause_after_ms=0, is_beep=True))
            else:
                chunks.append(TextChunk(text=text.strip(), pause_after_ms=0))

        split_ms = round((time.time() - t_start) * 1000, 2)
        for c in chunks:
            c.split_ms = split_ms

        return chunks

    @staticmethod
    def _split_plain_text(
        text: str,
        next_is_beep: bool = False,
        max_first_chunk_chars: Optional[int] = None,
        max_chunk_chars: int = _MAX_CHUNK_CHARS,
        is_first_plain_segment: bool = True
    ) -> List['TextChunk']:
        """
        純粋な自然言語テキスト（メタタグを含まないプレーンテキスト）を、
        句読点・読点・スペース・スラング・語尾・形態素・動的ハードキャップで文分割する。
        """
        if not text or not text.strip():
            return []

        # 1. 本文の文境界分割 ([。！？]+)
        raw_parts = _RE_SPLIT_PUNCT.split(text)
        current_sentences: List[Tuple[str, str]] = []
        for i in range(0, len(raw_parts), 2):
            part = raw_parts[i].strip()
            delim = raw_parts[i+1].strip() if i+1 < len(raw_parts) else ""
            full = (part + delim).strip()
            if full:
                current_sentences.append((full, delim))

        # 2. 定型挨拶・相槌の文頭/文末スライス (キャッシュ再利用性最大化)
        greeting_refined: List[Tuple[str, str]] = []
        for sentence, delim in current_sentences:
            greeting_refined.extend(TextCleaner._split_by_greetings_boundary(sentence, delim))

        # 3. 文頭の接続詞・相槌スライス (先頭チャンクのTTFAゼロ遅延化)
        head_conj_refined: List[Tuple[str, str]] = []
        for idx, (sentence, delim) in enumerate(greeting_refined):
            if idx == 0:
                head_conj_refined.extend(TextCleaner._split_by_head_conjunction(sentence, delim))
            else:
                head_conj_refined.append((sentence, delim))

        # 4. 読点境界（「、」）での分割 (動的短文時は低閾値連動)
        comma_refined: List[Tuple[str, str]] = []
        for idx, (sentence, delim) in enumerate(head_conj_refined):
            raw_len = len(_RE_STRIP_PUNCT_SPACES.sub('', sentence))
            cur_limit = (max_first_chunk_chars if (idx == 0 and is_first_plain_segment and max_first_chunk_chars) else max_chunk_chars)
            comma_thresh = min(15, max(6, int(cur_limit * 0.55)))
            if idx == 0 and is_first_plain_segment and max_first_chunk_chars and max_first_chunk_chars < 15:
                comma_thresh = min(comma_thresh, max_first_chunk_chars)
            if '、' in sentence and raw_len > comma_thresh:
                sub_parts = _RE_SPLIT_COMMA.split(sentence)
                temp_pieces: List[Tuple[str, str]] = []
                for j in range(0, len(sub_parts), 2):
                    sub_p = sub_parts[j].strip()
                    sub_d = sub_parts[j+1].strip() if j+1 < len(sub_parts) else ""
                    sub_full = (sub_p + sub_d).strip()
                    if sub_full:
                        temp_pieces.append((sub_full, sub_d or delim))
                
                # 3文字以上の実質的パーツに分割できる場合は採用
                if len(temp_pieces) > 1 and all(len(_RE_STRIP_PUNCT_SPACES.sub('', p[0])) >= 3 for p in temp_pieces):
                    comma_refined.extend(temp_pieces)
                else:
                    comma_refined.append((sentence, delim))
            else:
                comma_refined.append((sentence, delim))

        # 3. スペース（空白）境界分割 (動的閾値連動、英単語間スペース保護、O(N)スキャン)
        space_refined: List[Tuple[str, str]] = []
        for idx, (sentence, delim) in enumerate(comma_refined):
            raw_len = len(_RE_STRIP_PUNCT_SPACES.sub('', sentence))
            cur_limit = (max_first_chunk_chars if (idx == 0 and is_first_plain_segment and max_first_chunk_chars) else max_chunk_chars)
            space_thresh = min(14, max(6, int(cur_limit * 0.5)))
            if raw_len > space_thresh:
                s_parts = TextCleaner._split_by_space_boundary(sentence)
                if len(s_parts) > 1 and all(len(_RE_STRIP_PUNCT_SPACES.sub('', p)) >= _MIN_PART_CHARS for p in s_parts):
                    for p_idx, p in enumerate(s_parts):
                        p_delim = delim if p_idx == len(s_parts) - 1 else "space"
                        space_refined.append((p.strip(), p_delim))
                else:
                    space_refined.append((sentence, delim))
            else:
                space_refined.append((sentence, delim))

        # 4. スラング・笑い境界分割 (動的閾値連動)
        slang_refined: List[Tuple[str, str]] = []
        for idx, (sentence, delim) in enumerate(space_refined):
            raw_len = len(_RE_STRIP_PUNCT_SPACES.sub('', sentence))
            cur_limit = (max_first_chunk_chars if (idx == 0 and is_first_plain_segment and max_first_chunk_chars) else max_chunk_chars)
            slang_thresh = min(14, max(6, int(cur_limit * 0.5)))
            if raw_len > slang_thresh:
                slang_pieces = TextCleaner._split_by_slang_boundary(sentence, delim)
                slang_refined.extend(slang_pieces)
            else:
                slang_refined.append((sentence, delim))

        # 5. 語尾・終助詞・丁寧語終止形境界 (動的閾値連動、句読点なし文)
        ending_refined: List[Tuple[str, str]] = []
        for idx, (sentence, delim) in enumerate(slang_refined):
            raw_len = len(_RE_STRIP_PUNCT_SPACES.sub('', sentence))
            cur_limit = (max_first_chunk_chars if (idx == 0 and is_first_plain_segment and max_first_chunk_chars) else max_chunk_chars)
            ending_thresh = min(16, max(7, int(cur_limit * 0.55)))
            end_min_part = min(6, max(3, ending_thresh // 3))
            if raw_len > ending_thresh:
                end_parts = _RE_SPLIT_ENDING.split(sentence)
                if len(end_parts) > 1:
                    e_buf = ""
                    e_pieces: List[Tuple[str, str]] = []
                    for e_idx, e_p in enumerate(end_parts):
                        e_buf += e_p
                        if e_idx % 2 == 1: # キャプチャされた語尾
                            if len(e_buf) >= end_min_part and (len(sentence) - len("".join(end_parts[:e_idx+1]))) >= end_min_part:
                                e_pieces.append((e_buf.strip(), "ending"))
                                e_buf = ""
                    if e_buf.strip():
                        e_pieces.append((e_buf.strip(), delim))
                    if len(e_pieces) > 1:
                        ending_refined.extend(e_pieces)
                    else:
                        ending_refined.append((sentence, delim))
                else:
                    ending_refined.append((sentence, delim))
            else:
                ending_refined.append((sentence, delim))

        # 6. 長文に対する形態素（SudachiPy / Janome / 接続助詞・接続詞）での分割 (全チャンク動的閾値連動)
        morph_refined: List[Tuple[str, str]] = []
        for idx, (sentence, delim) in enumerate(ending_refined):
            raw_len = len(_RE_STRIP_PUNCT_SPACES.sub('', sentence))
            cur_limit = (max_first_chunk_chars if (idx == 0 and is_first_plain_segment and max_first_chunk_chars) else max_chunk_chars)
            if cur_limit < 22:
                m_thresh = max(8, min(18, int(cur_limit * 0.75)))
                if idx == 0 and is_first_plain_segment and max_first_chunk_chars and max_first_chunk_chars < 18:
                    m_thresh = max(8, min(m_thresh, max_first_chunk_chars))
                m_head = min(7, max(3, m_thresh // 2))
                m_tail = min(5, max(3, m_thresh // 3))
            else:
                m_thresh = 18
                m_head = 7
                m_tail = 5

            if raw_len >= m_thresh:
                m_pieces = TextCleaner._split_long_sentence_morphological(
                    sentence, delim,
                    min_chars_threshold=m_thresh,
                    min_head_chars=m_head,
                    min_tail_chars=m_tail
                )
                morph_refined.extend(m_pieces)
            else:
                morph_refined.append((sentence, delim))

        # 7. 動的ハードキャップ安全スライス (最終防衛線: 第1文は動的上限、以降は通常上限)
        final_sentences: List[Tuple[str, str]] = []
        for idx, (sentence, delim) in enumerate(morph_refined):
            cur_max = (max_first_chunk_chars if (idx == 0 and is_first_plain_segment and max_first_chunk_chars) else max_chunk_chars)
            hardcap_pieces = TextCleaner._hardcap_slice(sentence, delim, max_chars=cur_max)
            final_sentences.extend(hardcap_pieces)

        # 8. TextChunk の構築
        chunks: List[TextChunk] = []
        for idx, (sentence, delim) in enumerate(final_sentences):
            is_last = (idx == len(final_sentences) - 1)
            if is_last:
                pause_ms = 20 if next_is_beep else 0
            elif '。' in delim:
                pause_ms = 150
            elif '！' in delim or '？' in delim:
                pause_ms = 120
            elif '、' in delim:
                pause_ms = 90
            elif delim == 'greeting' or delim == 'greeting_before':
                pause_ms = 90
            elif delim == 'head_conj':
                pause_ms = 85
            elif delim == 'space':
                pause_ms = 75
            elif delim == 'slang':
                pause_ms = 70
            elif delim == 'ending':
                pause_ms = 80
            elif delim == 'conj':
                pause_ms = 85
            elif delim == 'hardcap':
                pause_ms = 60
            else:
                pause_ms = 90

            is_followed = (is_last and next_is_beep)
            chunks.append(TextChunk(text=sentence, pause_after_ms=pause_ms, is_followed_by_beep=is_followed))

        # 9. 記号のみチャンクの安全統合 (Azure/Edge-TTSの44byte空音声・発音不能エラー完全防止)
        cleaned_chunks: List[TextChunk] = []
        for c in chunks:
            if _RE_HAS_READABLE.search(c.text):
                cleaned_chunks.append(c)
            else:
                if cleaned_chunks:
                    cleaned_chunks[-1].text += c.text
                    cleaned_chunks[-1].pause_after_ms = max(cleaned_chunks[-1].pause_after_ms, c.pause_after_ms)
                    cleaned_chunks[-1].is_followed_by_beep = c.is_followed_by_beep
                else:
                    cleaned_chunks.append(c)

        return cleaned_chunks

    @staticmethod
    def normalize_cache_key_text(text: str) -> str:
        """
        キャッシュキー（SHA-256）計算専用のテキスト正規化。
        音声合成時の感情表現や語尾ゆれ（！、ー、〜、w）を吸収し、キャッシュHIT率を最大化する。
        ※ イントネーションに影響する疑問符「？」は保持する。
        """
        if not text:
            return ""
        t = text.strip()

        # 1. 頻出挨拶・相槌のかな/漢字表記ゆれの統一
        greeting_map = {
            "おつかれさまでした": "お疲れ様でした",
            "おつかれさまです": "お疲れ様です",
            "おつかれさま": "お疲れ様",
            "おつかれ": "お疲れ",
            "おつ": "お疲れ",
            "ありがとうございました": "有難うございました",
            "ありがとうございます": "有難うございます",
            "よろしくおねがいします": "よろしくお願いします",
            "よろしくです": "よろしく",
            "りょうかいです": "了解です",
            "りょうかい": "了解",
        }
        base_no_marks = t.rstrip(_TRAILING_MARKS_CHARS)
        if base_no_marks in greeting_map:
            t = greeting_map[base_no_marks] + t[len(base_no_marks):]

        # 2. 疑問符の有無判定（イントネーション保持）
        has_question = any(c in _QUESTION_CHARS for c in t[-3:])

        # 3. 文末の連続する装飾記号（！、ー、〜、w、ｗ、。、空白、読点）を除去 (完全 O(N) rstrip)
        t = t.rstrip(_TRAILING_MARKS_NO_QUES_CHARS).strip()

        # 疑問符があった場合は「？」を1つだけ末尾に統一付与
        if has_question:
            t = t.rstrip(_QUESTION_CHARS).strip() + "？"

        return t
