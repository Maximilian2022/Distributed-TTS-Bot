import re
import gc
import os
import json
import time
import asyncio
import resource
import contextvars
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import emoji
import logging
import threading
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
import discord
from src.utils.streaming_audio import calc_beep_duration
from src.utils.latency_profiler import latency_profiler

logger = logging.getLogger(__name__)

# SudachiPy 形態素解析器のシングルトン保持用 (C-3)
_sudachi_tokenizer = None
_sudachi_failed = False
_sudachi_split_mode = None
_sudachi_lock = threading.Lock()

# O(1) 英語→カタカナ辞書 & e2k 未知語オフライン変換用シングルトン/LRUキャッシュ
_SYSTEM_ENGLISH_KANA_DICT_PATH = Path(__file__).resolve().parent / "english_kana_dict.json"
_ENGLISH_DICT_LOCK = threading.RLock()
_SYSTEM_ENGLISH_KANA_DICT: Dict[str, str] = {}
_SYSTEM_ENGLISH_KANA_META: Dict[str, Any] = {}
_CUSTOM_ENGLISH_KANA_DICT: Dict[str, str] = {}
_ENGLISH_DICTS_LOADED = False

# 全サーバー共通 日本語・ゲーム用語読み辞書 (単一パス複合正規表現 + O(1) ルックアップ)
_SYSTEM_COMMON_READING_DICT_PATH = Path(__file__).resolve().parent / "common_reading_dict.json"
_COMMON_READING_LOCK = threading.RLock()
_SYSTEM_COMMON_READING_DICT: Dict[str, str] = {}
_SYSTEM_COMMON_READING_META: Dict[str, Any] = {}
_CUSTOM_COMMON_READING_DICT: Dict[str, str] = {}
_COMMON_READING_MERGED_LOWER: Dict[str, str] = {}
_COMMON_READING_REGEX: Optional[re.Pattern] = None
_COMMON_READING_LOADED = False

_RE_ASCII_ALPHA = re.compile(r'[a-zA-Z]')
_RE_HAS_ASCII_ALNUM = re.compile(r'[a-zA-Z0-9]')

_ALKANA_MODULE: Any = None
_ALKANA_CHECKED = False

_E2K_NGRAM: Any = None
_E2K_C2K: Any = None
_E2K_FAILED = False
_E2K_LOCK = threading.Lock()
_E2K_CACHE: "OrderedDict[str, str]" = OrderedDict()
_E2K_CACHE_MAX_SIZE = 10000
_E2K_MAX_INFER_PER_TEXT = 8

_RE_VALID_ENGLISH_WORD_KEY = re.compile(r'^[a-zA-Z]{2,64}$')
_RE_CTRL_CHARS = re.compile(r'[\x00-\x1f\x7f]')


def _get_custom_english_kana_dict_path() -> Path:
    override = os.getenv("TTSBOT_CUSTOM_ENGLISH_DICT_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "data" / "custom_english_kana.json"


def _load_english_kana_json(path: Path) -> Tuple[Dict[str, str], Dict[str, Any]]:
    if not path.exists():
        return {}, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"⚠️ [EnglishDict] 辞書JSONの読み込みに失敗しました ({path}): {e}")
        return {}, {}
    if not isinstance(raw, dict):
        return {}, {}

    meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
    entries_raw = raw.get("entries") if isinstance(raw.get("entries"), dict) else raw
    parsed: Dict[str, str] = {}
    for k, v in entries_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.startswith("_"):
            continue
        lk = k.strip().lower()
        rv = v.strip()
        if _RE_VALID_ENGLISH_WORD_KEY.match(lk) and rv and set(lk) != {"w"}:
            parsed[lk] = rv
    return parsed, meta


def ensure_english_kana_dicts_loaded(force: bool = False) -> None:
    """システム英語カタカナ辞書とカスタム英語カタカナ辞書をメモリへロードする (O(1)参照用)"""
    global _SYSTEM_ENGLISH_KANA_DICT, _SYSTEM_ENGLISH_KANA_META, _CUSTOM_ENGLISH_KANA_DICT, _ENGLISH_DICTS_LOADED
    if _ENGLISH_DICTS_LOADED and not force:
        return
    with _ENGLISH_DICT_LOCK:
        if _ENGLISH_DICTS_LOADED and not force:
            return
        sys_dict, sys_meta = _load_english_kana_json(_SYSTEM_ENGLISH_KANA_DICT_PATH)
        cust_dict, _ = _load_english_kana_json(_get_custom_english_kana_dict_path())
        _SYSTEM_ENGLISH_KANA_DICT = sys_dict
        _SYSTEM_ENGLISH_KANA_META = sys_meta
        _CUSTOM_ENGLISH_KANA_DICT = cust_dict
        _ENGLISH_DICTS_LOADED = True
        if force:
            with _E2K_LOCK:
                _E2K_CACHE.clear()


def _get_custom_common_reading_dict_path() -> Path:
    override = os.getenv("TTSBOT_CUSTOM_COMMON_DICT_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "data" / "custom_common_reading.json"


def _load_common_reading_json(path: Path) -> Tuple[Dict[str, str], Dict[str, Any]]:
    if not path.exists():
        return {}, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"⚠️ [CommonDict] 辞書JSONの読み込みに失敗しました ({path}): {e}")
        return {}, {}
    if not isinstance(raw, dict):
        return {}, {}

    meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
    entries_raw = raw.get("entries") if isinstance(raw.get("entries"), dict) else raw
    parsed: Dict[str, str] = {}
    for k, v in entries_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.startswith("_"):
            continue
        kw = _RE_CTRL_CHARS.sub("", k).strip()
        rv = _RE_CTRL_CHARS.sub("", v).strip()
        if 1 <= len(kw) <= 64 and 1 <= len(rv) <= 128:
            parsed[kw] = rv
    return parsed, meta


def _rebuild_common_reading_matcher_locked() -> None:
    """システム辞書とカスタム辞書を統合し、最長一致＋境界ガード付き単一正規表現を構築"""
    global _COMMON_READING_MERGED_LOWER, _COMMON_READING_REGEX

    merged: Dict[str, str] = dict(_SYSTEM_COMMON_READING_DICT)
    # カスタム登録がシステム初期値を上書き（大文字小文字が異なる同ASCII語もカスタム側優先）
    lower_to_canonical: Dict[str, str] = {k.lower(): k for k in merged}
    for ck, cv in _CUSTOM_COMMON_READING_DICT.items():
        lk = ck.lower()
        old_key = lower_to_canonical.get(lk)
        if old_key and old_key != ck:
            merged.pop(old_key, None)
        merged[ck] = cv
        lower_to_canonical[lk] = ck

    if not merged:
        _COMMON_READING_MERGED_LOWER = {}
        _COMMON_READING_REGEX = None
        return

    sorted_keys = sorted(merged.keys(), key=len, reverse=True)
    parts: List[str] = []
    lookup_lower: Dict[str, str] = {}

    for kw in sorted_keys:
        reading = merged[kw]
        lookup_lower[kw.lower()] = reading
        esc = re.escape(kw)
        if _RE_KANJI_ONLY.match(kw):
            pat_part = rf'(?<![\u4e00-\u9faf]){esc}(?![\u4e00-\u9faf])'
        elif _RE_ALNUM_ONLY.match(kw):
            pat_part = rf'(?<![a-zA-Z0-9_])(?i:{esc})(?![a-zA-Z0-9_])'
        elif _RE_HAS_ASCII_ALNUM.search(kw):
            prefix = r'(?<![a-zA-Z0-9_])' if _RE_HAS_ASCII_ALNUM.match(kw[0]) else ''
            suffix = r'(?![a-zA-Z0-9_])' if _RE_HAS_ASCII_ALNUM.match(kw[-1]) else ''
            body = f'(?i:{esc})' if _RE_ASCII_ALPHA.search(kw) else esc
            pat_part = f'{prefix}{body}{suffix}'
        else:
            pat_part = esc
        parts.append(pat_part)

    _COMMON_READING_MERGED_LOWER = lookup_lower
    _COMMON_READING_REGEX = re.compile("|".join(parts))


def ensure_common_reading_dicts_loaded(force: bool = False) -> None:
    """全サーバー共通の日本語・ゲーム用語読み辞書をロードして事前コンパイルする"""
    global _SYSTEM_COMMON_READING_DICT, _SYSTEM_COMMON_READING_META, _CUSTOM_COMMON_READING_DICT, _COMMON_READING_LOADED
    if _COMMON_READING_LOADED and not force:
        return
    with _COMMON_READING_LOCK:
        if _COMMON_READING_LOADED and not force:
            return
        sys_dict, sys_meta = _load_common_reading_json(_SYSTEM_COMMON_READING_DICT_PATH)
        cust_dict, _ = _load_common_reading_json(_get_custom_common_reading_dict_path())
        _SYSTEM_COMMON_READING_DICT = sys_dict
        _SYSTEM_COMMON_READING_META = sys_meta
        _CUSTOM_COMMON_READING_DICT = cust_dict
        _rebuild_common_reading_matcher_locked()
        _COMMON_READING_LOADED = True


def _get_alkana():
    """alkana モジュールを1回だけ解決してキャッシュする"""
    global _ALKANA_MODULE, _ALKANA_CHECKED
    if _ALKANA_CHECKED:
        return _ALKANA_MODULE
    try:
        import alkana
        _ALKANA_MODULE = alkana
    except ImportError:
        _ALKANA_MODULE = None
    _ALKANA_CHECKED = True
    return _ALKANA_MODULE


def _get_e2k_converters():
    """e2k (Public Domain) の NGram / C2K モデルをスレッドセーフに遅延/事前初期化する"""
    global _E2K_NGRAM, _E2K_C2K, _E2K_FAILED
    if _E2K_NGRAM is not None and _E2K_C2K is not None:
        return _E2K_NGRAM, _E2K_C2K
    if _E2K_FAILED:
        return None, None
    with _E2K_LOCK:
        if _E2K_NGRAM is not None and _E2K_C2K is not None:
            return _E2K_NGRAM, _E2K_C2K
        if _E2K_FAILED:
            return None, None
        try:
            from e2k import C2K, NGram
            _E2K_NGRAM = NGram()
            _E2K_C2K = C2K()
            logger.info("✨ [TextCleaner] オフライン未知英単語変換エンジン e2k (NGram/C2K) をロードしました。")
        except Exception as e:
            _E2K_FAILED = True
            logger.debug(f"e2k converter unavailable, skipping offline fallback: {e}")
            return None, None
    return _E2K_NGRAM, _E2K_C2K

# 高速化: 辞書置換および文分割用の正規表現キャッシュ & 事前コンパイル定数 (PERF-1, PERF-2)
_DICT_REGEX_CACHE: Dict[Tuple[str, str], re.Pattern] = {}
_SPLIT_CONJ_REGEX_CACHE: Dict[int, re.Pattern] = {}
_RE_ENGLISH_WORD = re.compile(r'(?<![a-zA-Z0-9_])[a-zA-Z]{2,}(?![a-zA-Z0-9_])')
_RE_SPLIT_PUNCT = re.compile(r'([。！？]+[）\)\]」』】]*[\s]*)')
_RE_SPLIT_COMMA = re.compile(r'(、+)')
_RE_STRIP_PUNCT_SPACES = re.compile(r'[、。！？\s]')
_RE_KANJI_ONLY = re.compile(r'^[\u4e00-\u9faf]+$')
_RE_ALNUM_ONLY = re.compile(r'^[a-zA-Z0-9]+$')
_RE_HAS_READABLE = re.compile(r'[a-zA-Z0-9ａ-ｚＡ-Ｚ０-９\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fd-\u30ff\u4e00-\u9faf\u3400-\u4dbf々]')
_RE_CONTAINS_KANJI = re.compile(r'[\u4e00-\u9faf\u3400-\u4dbf々ヶ]')
_RE_HAS_DIGITS = re.compile(r'[0-9０-９]')
_RE_KANJI_NUM_ONLY = re.compile(r'^[一二三四五六七八九十百千万億兆〇]+$')
_RE_VALID_KATAKANA_READING = re.compile(r'^[\u30a1-\u30f6ー]+$')
_RE_GDICT_PLACEHOLDER = re.compile(r'[\ue000-\uefff]|\x00GDICT_(\d+)\x00')
_GUILD_DICT_CACHE_LOCK = threading.Lock()
_GUILD_DICT_MATCHER_CACHE: "OrderedDict[Tuple[Tuple[str, str], ...], Tuple[Optional[re.Pattern], Dict[str, str], Dict[str, str]]]" = OrderedDict()
_GUILD_DICT_CACHE_MAX = 128


def _get_compiled_guild_dict_matcher(
    dictionary_entries: List[Tuple[str, str]]
) -> Tuple[Optional[re.Pattern], Dict[str, str], Dict[str, str]]:
    """サーバー個別辞書エントリ列から最長一致・境界ガード付き単一正規表現をLRUキャッシュ取得する"""
    norm_key = tuple((w.strip(), r) for w, r in dictionary_entries if w and w.strip())
    if not norm_key:
        return None, {}, {}
    with _GUILD_DICT_CACHE_LOCK:
        cached = _GUILD_DICT_MATCHER_CACHE.get(norm_key)
        if cached is not None:
            _GUILD_DICT_MATCHER_CACHE.move_to_end(norm_key)
            return cached

    sorted_entries = sorted(norm_key, key=lambda x: len(x[0]), reverse=True)
    parts: List[str] = []
    exact_map: Dict[str, str] = {}
    lower_map: Dict[str, str] = {}
    seen_words = set()

    for w, reading in sorted_entries:
        is_alnum = bool(_RE_ALNUM_ONLY.match(w))
        dedup_k = ("ci", w.lower()) if is_alnum else ("cs", w)
        if dedup_k in seen_words:
            continue
        seen_words.add(dedup_k)

        esc = re.escape(w)
        if _RE_KANJI_ONLY.match(w):
            exact_map.setdefault(w, reading)
            parts.append(rf'(?<![\u4e00-\u9faf]){esc}(?![\u4e00-\u9faf])')
        elif is_alnum:
            lower_map.setdefault(w.lower(), reading)
            parts.append(rf'(?<![a-zA-Z0-9_])(?i:{esc})(?![a-zA-Z0-9_])')
        else:
            exact_map.setdefault(w, reading)
            parts.append(esc)

    compiled = (re.compile("|".join(parts)) if parts else None, exact_map, lower_map)
    with _GUILD_DICT_CACHE_LOCK:
        _GUILD_DICT_MATCHER_CACHE[norm_key] = compiled
        while len(_GUILD_DICT_MATCHER_CACHE) > _GUILD_DICT_CACHE_MAX:
            _GUILD_DICT_MATCHER_CACHE.popitem(last=False)
    return compiled


def _build_kata_to_hira_trans() -> Dict[int, str]:
    table: Dict[int, str] = {}
    for cp in range(0x30A1, 0x30F5):  # ァ(30A1)..ヴ(30F4) -> ぁ(3041)..ゔ(3094)
        table[cp] = chr(cp - 0x60)
    table[0x30F5] = "か"  # ヵ
    table[0x30F6] = "け"  # ヶ
    return str.maketrans(table)


_KATA_TO_HIRA_TRANS = _build_kata_to_hira_trans()

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
_FASTPATH_MAX_CHARS = 14  # 14文字以下の短文は形態素解析をスキップするファストパス上限 (CPU縮退時でも自力完走可能)
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

# 形態素解析を使わず正規表現のみで分割させるフラグ (split_into_chunks_async のタイムアウト退避用)
_skip_morph: contextvars.ContextVar = contextvars.ContextVar("skip_morph", default=False)

def get_sudachi_tokenizer():
    """SudachiPy 形態素解析器をスレッドセーフに遅延初期化して返却"""
    if _skip_morph.get():
        return None, None
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
    if _skip_morph.get():
        return None
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

    # 3. O(1) 英語カタカナ辞書・共通日本語辞書・alkana・e2k 未知語変換エンジンの事前ウォームアップ
    try:
        ensure_english_kana_dicts_loaded()
        ensure_common_reading_dicts_loaded()
        _get_alkana()
        ngram_fn, c2k_fn = _get_e2k_converters()
        if ngram_fn is not None and c2k_fn is not None:
            _ = c2k_fn("english")
            _ = ngram_fn("api")
    except Exception as e:
        logger.debug(f"English/e2k/CommonDict warm-up skipped: {e}")

    # 4. TextCleaner の文章分割パイプライン全体のホットウォーム
    try:
        for sample in warm_samples:
            _ = TextCleaner.split_into_chunks(sample, author_prefix="テストユーザー")
    except Exception as e:
        logger.debug(f"TextCleaner warm-up error: {e}")

    elapsed_ms = (time.perf_counter() - t_start) * 1000.0
    logger.info(
        f"✨ [TextCleaner] 形態素解析・英語辞書ホットウォーム完了 "
        f"(SudachiPy/Janome/Dict={len(_SYSTEM_ENGLISH_KANA_DICT)}+{len(_CUSTOM_ENGLISH_KANA_DICT)}, 所要時間: {elapsed_ms:.1f}ms)"
    )

_SPLIT_DIAG_THRESHOLD_MS = 500.0
_SPLIT_TIMEOUT_S = 0.3
_SPLIT_EXECUTOR = ThreadPoolExecutor(max_workers=min(8, max(4, os.cpu_count() or 4)), thread_name_prefix="text-split")
_SPLIT_FALLBACK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="text-split-fb")

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

        # 5. 単語辞書置換 (サーバー個別辞書優先 + 全サーバー共通日本語/ゲーム用語辞書)
        text = self.apply_dictionary(text, dictionary_entries)

        # 6. alkana による英単語即時カタカナ変換 & 未知語検知
        text, detected_unknown = self.convert_english_to_kana(text)
        self.last_detected_unknown_words = detected_unknown

        # 7. スラング・表記揺れの正規化 (辞書置換後に適用)
        text = self.normalize_slang(text)

        # 改行・連続空白の整理 (改行は文境界として句点に置換)
        text = self._newline_pattern.sub("。 ", text)
        text = self._space_pattern.sub(" ", text).strip()

        # 8. 文字数制限 (超過部分は切り捨てて「以下略」を付与。NGワードBEEPタグの途中切断を防止)
        limit = max_length if max_length is not None else self.max_length
        if len(text) > limit:
            cut_pos = limit
            for m in _RE_BEEP_TAG.finditer(text):
                if m.start() < cut_pos < m.end():
                    cut_pos = m.end()
                    break
            if len(text) > cut_pos:
                text = text[:cut_pos].rstrip() + " 以下略"

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
    def apply_dictionary(cls, text: str, dictionary_entries: Optional[List[Tuple[str, str]]] = None) -> str:
        """
        単語辞書置換 (サーバー個別辞書優先 -> 全サーバー共通日本語/ゲーム用語辞書)
        最長一致 (Longest-Match First) & 誤爆防止境界ガード付き。
        サーバー辞書で確定した置換箇所は一時プレースホルダで保護し、共通辞書による二重置換・上書きを防止する。
        """
        ensure_common_reading_dicts_loaded()
        if not dictionary_entries and _COMMON_READING_REGEX is None:
            return text

        protected_replacements: List[str] = []

        if dictionary_entries:
            guild_regex, guild_exact, guild_lower = _get_compiled_guild_dict_matcher(dictionary_entries)
            if guild_regex is not None and text:
                def _replace_guild_match(m: re.Match) -> str:
                    matched = m.group(0)
                    reading_val = guild_exact.get(matched)
                    if reading_val is None:
                        reading_val = guild_lower.get(matched.lower(), matched)
                    if _COMMON_READING_REGEX is None:
                        return reading_val
                    idx = len(protected_replacements)
                    protected_replacements.append(reading_val)
                    if idx < 0x1000:
                        return chr(0xE000 + idx)
                    return f"\x00GDICT_{idx}\x00"

                text = guild_regex.sub(_replace_guild_match, text)

        # 全サーバー共通日本語/ゲーム用語辞書 (単一パス複合正規表現 + O(1) ルックアップ)
        with _COMMON_READING_LOCK:
            common_regex = _COMMON_READING_REGEX
            common_lookup = _COMMON_READING_MERGED_LOWER

        if common_regex is not None and text:
            text = common_regex.sub(
                lambda m: common_lookup.get(m.group(0).lower(), m.group(0)),
                text
            )

        if protected_replacements:
            def _restore_placeholder(m: re.Match) -> str:
                tok = m.group(0)
                if len(tok) == 1:
                    idx = ord(tok) - 0xE000
                else:
                    idx = int(m.group(1))
                return protected_replacements[idx] if 0 <= idx < len(protected_replacements) else tok

            text = _RE_GDICT_PLACEHOLDER.sub(_restore_placeholder, text)

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
        "tps": "ティーピーエス",
        "rpg": "アールピージー",
        "mmo": "エムエムオー",
        "pvp": "ピーブイピー",
        "pve": "ピーブイイー",
        "rta": "アールティーエー",
        "bot": "ボット",
        "ai": "エーアイ",
        "ui": "ユーアイ",
        "api": "エーピーアイ",
        "web": "ウェブ",
        "sns": "エスエヌエス",
        "cpu": "シーピーユー",
        "gpu": "ジーピーユー",
        "ram": "ラム",
        "rom": "ロム",
        "ssd": "エスエスディー",
        "hdd": "エイチディーディー",
        "os": "オーエス",
        "vr": "ブイアール",
        "ar": "エーアール",
        "bgm": "ビージーエム",
        "gg": "ジージー",
        "afk": "エーエフケー",
        "brb": "ビーアールビー",
        "ps": "プレイステーション",
        "switch": "スイッチ",
        "xbox": "エックスボックス",
        "steam": "スチーム",
        "discord": "ディスコード",
        "twitter": "ツイッター",
        "youtube": "ユーチューブ",
        "twitch": "ツイッチ",
        "tiktok": "ティックトック",
        "instagram": "インスタグラム",
        "niconico": "ニコニコ",
        "bilibili": "ビリビリ",
        "vtuber": "ブイチューバー",
        "github": "ギットハブ",
        "chatgpt": "チャットジーピーティー",
        "openai": "オープンエーアイ",
        "netflix": "ネットフリックス",
        "amazon": "アマゾン",
        "spotify": "スポティファイ",
        "obs": "オービーエス",
        "iphone": "アイフォン",
        "ipad": "アイパッド",
        "ios": "アイオーエス",
        "android": "アンドロイド",
        "valorant": "ヴァロラント",
        "minecraft": "マインクラフト",
        "fortnite": "フォートナイト",
        "lol": "ロル",
        "vrchat": "ブイアールチャット",
        "reddit": "レディット",
        "wikipedia": "ウィキペディア",
        "alt": "オルト",
        "ctrl": "コントロール",
        "esc": "エスケープ",
        "tab": "タブ",
        "shift": "シフト",
        "atm": "エーティーエム",
        "vpn": "ブイピーエヌ",
        "dns": "ディーエヌエス",
        "http": "エイチティーティーピー",
        "https": "エイチティーティーピーエス",
        "html": "エイチティーエムエル",
        "css": "シーエスエス",
        "json": "ジェイソン",
        "yaml": "ヤムル",
        "sql": "エスキューエル",
        "ssh": "エスエスエイチ",
        "usb": "ユーエスビー",
        "hdmi": "エイチディーエムアイ",
        "lan": "ラン",
        "wan": "ワン",
        "wifi": "ワイファイ",
        "docker": "ドッカー",
        "linux": "リナックス",
        "windows": "ウィンドウズ",
        "pokemon": "ポケモン",
        # MMO / ネットゲーム / チャット略語 (WIKIWIKI MMO用語全集 & 実測誤読対策)
        "directx": "ダイレクトエックス",
        "wb": "ダブリュービー",
        "debuff": "デバフ",
        "buff": "バフ",
        "mob": "モブ",
        "melee": "メレー",
        "dupe": "デュープ",
        "thx": "サンクス",
        "thanx": "サンクス",
        "plz": "プリーズ",
        "ez": "イージー",
        "rdy": "レディ",
        "cya": "シーヤー",
        "cul": "シーユーエル",
        "hehe": "ヘヘ",
        "gj": "ジージェー",
        "omg": "オーエムジー",
        "omw": "オーエムダブリュー",
        "lfg": "エルエフジー",
        "ld": "エルディー",
        "asap": "エーエスエーピー",
        "afaik": "エーエフエーアイケー",
        "btw": "ビーティーダブリュー",
        "pk": "ピーケー",
        "pkk": "ピーケーケー",
        "fpk": "エフピーケー",
        "pt": "ピーティー",
        "npc": "エヌピーシー",
        "gm": "ジーエム",
        "hp": "エイチピー",
        "mp": "エムピー",
        "str": "エスティーアール",
        "dex": "デックス",
        "vit": "ビット",
        "int": "イント",
        "mnd": "エムエヌディー",
        "chr": "シーエイチアール",
        "ddos": "ディードス",
        "rmt": "アールエムティー",
        "rmter": "アールエムター",
        "ss": "エスエス",
        "hq": "エイチキュー",
        "dmg": "ディーエムジー",
        "wtb": "ダブリューティービー",
        "wts": "ダブリューティーエス",
        "wtt": "ダブリューティーティー",
        "wiz": "ウィズ",
        "np": "エヌピー",
        "dk": "ディーケー",
        "dyk": "ディーワイケー",
        "dyt": "ディーワイティー",
        "ack": "エーシーケー",
        "bibi": "バイバイ",
        "fsaa": "エフエスエーエー",
        "morpg": "エムオーアールピージー",
        "mmorpg": "エムエムオーアールピージー",
        "rvr": "アールブイアール",
        "kos": "ケーオーエス",
        "ks": "ケーエス",
        "pol": "ピーオーエル",
        "poof": "プーフ",
        "twink": "トゥインク",
        "evac": "イバック",
        "kwel": "クール",
    }

    @classmethod
    def resolve_english_word_kana(
        cls,
        raw_w: str,
        *,
        allow_e2k: bool = True,
        e2k_budget: Optional[List[int]] = None,
    ) -> Tuple[Optional[str], str]:
        """
        英単語1語を多層 O(1) 辞書 + オフライン未知語変換 (e2k + LRU) で解決する。
        Returns: (カタカナ読み or None, 解決元 tier)
        tier: 'slang_skip' | 'custom' | 'builtin' | 'alkana' | 'system' | 'e2k_cache' | 'e2k' | 'unknown'
        """
        lower_w = raw_w.lower()

        # 'www' など w の連続ネットスラングは後段 normalize_slang のためそのまま通す
        if set(lower_w) == {"w"}:
            return None, "slang_skip"

        ensure_english_kana_dicts_loaded()

        # 1. カスタム共通辞書 (WebUI・管理者設定、最優先 O(1))
        custom_hit = _CUSTOM_ENGLISH_KANA_DICT.get(lower_w)
        if custom_hit:
            return custom_hit, "custom"

        # 2. 組込定番略語・固有名詞マップ (O(1))
        builtin_hit = cls.COMMON_ACRONYMS.get(lower_w)
        if builtin_hit:
            return builtin_hit, "builtin"

        # 3. alkana 一般英単語辞書 (49,216語, O(1))
        alkana_mod = _get_alkana()
        if alkana_mod is not None:
            kana = alkana_mod.get_kana(lower_w)
            if kana:
                return kana, "alkana"

        # 4. 事前構築済みシステム辞書 JSON (SudachiDict抽出等, O(1))
        system_hit = _SYSTEM_ENGLISH_KANA_DICT.get(lower_w)
        if system_hit:
            return system_hit, "system"

        # 5. e2k LRU キャッシュ (O(1))
        with _E2K_LOCK:
            cached = _E2K_CACHE.get(lower_w)
            if cached:
                _E2K_CACHE.move_to_end(lower_w)
                return cached, "e2k_cache"

        # 6. e2k オフライン RNN 未知語即時変換 (Public Domain)
        if allow_e2k and (e2k_budget is None or e2k_budget[0] > 0):
            ngram_fn, c2k_fn = _get_e2k_converters()
            if ngram_fn is not None and c2k_fn is not None:
                try:
                    with _E2K_LOCK:
                        # ロック再取得時のダブルチェック
                        cached2 = _E2K_CACHE.get(lower_w)
                        if cached2:
                            _E2K_CACHE.move_to_end(lower_w)
                            return cached2, "e2k_cache"
                        inferred = c2k_fn(lower_w) if ngram_fn(lower_w) else ngram_fn.as_is(lower_w)
                        if inferred and isinstance(inferred, str) and inferred.strip():
                            inferred = inferred.strip()
                            _E2K_CACHE[lower_w] = inferred
                            if len(_E2K_CACHE) > _E2K_CACHE_MAX_SIZE:
                                _E2K_CACHE.popitem(last=False)
                            if e2k_budget is not None:
                                e2k_budget[0] -= 1
                            return inferred, "e2k"
                except Exception as e:
                    logger.debug(f"e2k inference skipped for {raw_w!r}: {e}")

        return None, "unknown"

    @classmethod
    def convert_english_to_kana(cls, text: str) -> Tuple[str, List[str]]:
        """
        英単語を多層 O(1) 辞書 (カスタム/組込/alkana/Sudachi抽出) および
        オフライン未知語推論 (e2k + LRUキャッシュ) を用いて即座にカタカナ化する。
        変換できなかった未登録の英字単語のみを unknown_words として返却する。
        """
        unknown_words: List[str] = []
        e2k_budget = [_E2K_MAX_INFER_PER_TEXT]

        def _replace_word(m: re.Match) -> str:
            raw_w = m.group(0)
            kana, tier = cls.resolve_english_word_kana(raw_w, allow_e2k=True, e2k_budget=e2k_budget)
            if kana:
                return kana
            if tier == "unknown" and len(raw_w) >= 2 and raw_w not in unknown_words:
                unknown_words.append(raw_w)
            return raw_w

        # 英単語境界にマッチ（英字2文字以上・事前コンパイル定数を使用）
        converted_text = _RE_ENGLISH_WORD.sub(_replace_word, text)
        return converted_text, unknown_words

    @classmethod
    def preview_english_kana(cls, text: str) -> Dict[str, Any]:
        """テキスト内の英単語ごとの変換結果と解決元 tier を診断付きで返す (WebUIテスト用)"""
        tokens: List[Dict[str, str]] = []
        e2k_budget = [_E2K_MAX_INFER_PER_TEXT]

        def _replace_word(m: re.Match) -> str:
            raw_w = m.group(0)
            kana, tier = cls.resolve_english_word_kana(raw_w, allow_e2k=True, e2k_budget=e2k_budget)
            out = kana if kana else raw_w
            tokens.append({"word": raw_w, "kana": out, "tier": tier})
            return out

        converted = _RE_ENGLISH_WORD.sub(_replace_word, text or "")
        return {
            "original": text or "",
            "converted": converted,
            "tokens": tokens,
        }

    # --- グローバル O(1) 英語カタカナ辞書の管理・永続化 API ---

    @classmethod
    def reload_english_kana_dict(cls) -> Dict[str, Any]:
        """ディスク上の英語カタカナ辞書を再ロードし、O(1)統計情報を返す"""
        ensure_english_kana_dicts_loaded(force=True)
        return cls.get_english_kana_stats()

    @classmethod
    def get_english_kana_stats(cls) -> Dict[str, Any]:
        ensure_english_kana_dicts_loaded()
        alkana_mod = _get_alkana()
        alkana_count = 0
        if alkana_mod is not None:
            data_mod = getattr(alkana_mod, "data", None)
            data_dict = getattr(data_mod, "data", None)
            if isinstance(data_dict, dict):
                alkana_count = len(data_dict)
        ngram_fn, c2k_fn = _get_e2k_converters() if not _E2K_FAILED else (None, None)
        with _ENGLISH_DICT_LOCK:
            custom_count = len(_CUSTOM_ENGLISH_KANA_DICT)
            system_count = len(_SYSTEM_ENGLISH_KANA_DICT)
            meta = dict(_SYSTEM_ENGLISH_KANA_META)
        with _E2K_LOCK:
            e2k_cache_count = len(_E2K_CACHE)
        return {
            "custom_count": custom_count,
            "builtin_count": len(cls.COMMON_ACRONYMS),
            "alkana_count": alkana_count,
            "system_count": system_count,
            "e2k_available": (ngram_fn is not None and c2k_fn is not None),
            "e2k_cache_count": e2k_cache_count,
            "system_meta": meta,
        }

    @classmethod
    def get_custom_english_kana_dict(cls) -> Dict[str, str]:
        ensure_english_kana_dicts_loaded()
        with _ENGLISH_DICT_LOCK:
            return dict(sorted(_CUSTOM_ENGLISH_KANA_DICT.items()))

    @classmethod
    def search_system_english_kana(cls, query: str = "", limit: int = 100) -> List[Dict[str, str]]:
        """システム/組込/カスタム辞書から条件一致するエントリを検索 (WebUIプレビュー用)"""
        ensure_english_kana_dicts_loaded()
        q = query.strip().lower()
        results: List[Dict[str, str]] = []
        seen = set()

        def _collect(mapping: Dict[str, str], source_name: str):
            for k in sorted(mapping.keys()):
                if len(results) >= limit:
                    return
                if k in seen:
                    continue
                v = mapping[k]
                if not q or (q in k) or (q in v.lower()):
                    seen.add(k)
                    results.append({"word": k, "reading": v, "source": source_name})

        with _ENGLISH_DICT_LOCK:
            _collect(_CUSTOM_ENGLISH_KANA_DICT, "custom")
            _collect(cls.COMMON_ACRONYMS, "builtin")
            _collect(_SYSTEM_ENGLISH_KANA_DICT, "system")
        return results

    @staticmethod
    def validate_english_kana_entry(word: str, reading: str) -> Tuple[str, str]:
        w = _RE_CTRL_CHARS.sub("", (word or "")).strip().lower()
        r = _RE_CTRL_CHARS.sub("", (reading or "")).strip()
        if not _RE_VALID_ENGLISH_WORD_KEY.match(w) or set(w) == {"w"}:
            raise ValueError("英単語は2〜64文字の半角英字 (a-z) で指定してください（www等は不可）。")
        if not r or len(r) > 128:
            raise ValueError("読み方は1〜128文字で入力してください。")
        return w, r

    @classmethod
    def _save_custom_english_kana_locked(cls, mapping: Dict[str, str]) -> None:
        path = _get_custom_english_kana_dict_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_meta": {"count": len(mapping)},
            "entries": dict(sorted(mapping.items())),
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    @classmethod
    def upsert_custom_english_kana(cls, word: str, reading: str) -> Tuple[str, str]:
        global _CUSTOM_ENGLISH_KANA_DICT
        w, r = cls.validate_english_kana_entry(word, reading)
        ensure_english_kana_dicts_loaded()
        with _ENGLISH_DICT_LOCK:
            new_map = dict(_CUSTOM_ENGLISH_KANA_DICT)
            new_map[w] = r
            cls._save_custom_english_kana_locked(new_map)
            _CUSTOM_ENGLISH_KANA_DICT = new_map
        with _E2K_LOCK:
            _E2K_CACHE.pop(w, None)
        return w, r

    @classmethod
    def import_custom_english_kana_batch(cls, items: List[Tuple[str, str]]) -> int:
        global _CUSTOM_ENGLISH_KANA_DICT
        ensure_english_kana_dicts_loaded()
        validated: List[Tuple[str, str]] = []
        for raw_w, raw_r in items:
            validated.append(cls.validate_english_kana_entry(raw_w, raw_r))
        if not validated:
            return 0
        with _ENGLISH_DICT_LOCK:
            new_map = dict(_CUSTOM_ENGLISH_KANA_DICT)
            for w, r in validated:
                new_map[w] = r
            cls._save_custom_english_kana_locked(new_map)
            _CUSTOM_ENGLISH_KANA_DICT = new_map
        with _E2K_LOCK:
            for w, _ in validated:
                _E2K_CACHE.pop(w, None)
        return len(validated)

    @classmethod
    def delete_custom_english_kana(cls, word: str) -> bool:
        global _CUSTOM_ENGLISH_KANA_DICT
        w = (word or "").strip().lower()
        if not w:
            return False
        ensure_english_kana_dicts_loaded()
        with _ENGLISH_DICT_LOCK:
            if w not in _CUSTOM_ENGLISH_KANA_DICT:
                return False
            new_map = dict(_CUSTOM_ENGLISH_KANA_DICT)
            del new_map[w]
            cls._save_custom_english_kana_locked(new_map)
            _CUSTOM_ENGLISH_KANA_DICT = new_map
        return True

    # --- 全サーバー共通 日本語・ゲーム用語読み辞書の管理・永続化 API ---

    @classmethod
    def reload_common_reading_dict(cls) -> Dict[str, Any]:
        """ディスク上の共通日本語・ゲーム用語辞書を再ロードする"""
        ensure_common_reading_dicts_loaded(force=True)
        return cls.get_common_reading_stats()

    @classmethod
    def get_common_reading_stats(cls) -> Dict[str, Any]:
        ensure_common_reading_dicts_loaded()
        with _COMMON_READING_LOCK:
            return {
                "custom_count": len(_CUSTOM_COMMON_READING_DICT),
                "system_count": len(_SYSTEM_COMMON_READING_DICT),
                "merged_count": len(_COMMON_READING_MERGED_LOWER),
                "system_meta": dict(_SYSTEM_COMMON_READING_META),
            }

    @classmethod
    def get_custom_common_reading_dict(cls) -> Dict[str, str]:
        ensure_common_reading_dicts_loaded()
        with _COMMON_READING_LOCK:
            return dict(sorted(_CUSTOM_COMMON_READING_DICT.items()))

    @classmethod
    def search_common_reading_dict(cls, query: str = "", limit: int = 100) -> List[Dict[str, str]]:
        """共通日本語・ゲーム用語辞書 (カスタム・システム初期辞書) を検索"""
        ensure_common_reading_dicts_loaded()
        q = _RE_CTRL_CHARS.sub("", (query or "")).strip().lower()
        results: List[Dict[str, str]] = []
        seen_lower = set()

        def _collect(mapping: Dict[str, str], source_name: str):
            for k in sorted(mapping.keys()):
                if len(results) >= limit:
                    return
                lk = k.lower()
                if lk in seen_lower:
                    continue
                v = mapping[k]
                if not q or (q in lk) or (q in v.lower()):
                    seen_lower.add(lk)
                    results.append({"word": k, "reading": v, "source": source_name})

        with _COMMON_READING_LOCK:
            _collect(_CUSTOM_COMMON_READING_DICT, "custom")
            _collect(_SYSTEM_COMMON_READING_DICT, "system")
        return results

    @staticmethod
    def validate_common_reading_entry(word: str, reading: str) -> Tuple[str, str]:
        w = _RE_CTRL_CHARS.sub("", (word or "")).strip()
        r = _RE_CTRL_CHARS.sub("", (reading or "")).strip()
        if not w or len(w) > 64:
            raise ValueError("単語は1〜64文字で入力してください。")
        if "\x00" in w or "\x00" in r:
            raise ValueError("不正な制御文字が含まれています。")
        if not r or len(r) > 128:
            raise ValueError("読み方は1〜128文字で入力してください。")
        return w, r

    @classmethod
    def _save_custom_common_reading_locked(cls, mapping: Dict[str, str]) -> None:
        path = _get_custom_common_reading_dict_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_meta": {"count": len(mapping)},
            "entries": dict(sorted(mapping.items())),
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    @classmethod
    def upsert_custom_common_reading(cls, word: str, reading: str) -> Tuple[str, str]:
        global _CUSTOM_COMMON_READING_DICT
        w, r = cls.validate_common_reading_entry(word, reading)
        ensure_common_reading_dicts_loaded()
        with _COMMON_READING_LOCK:
            new_map = dict(_CUSTOM_COMMON_READING_DICT)
            new_map[w] = r
            cls._save_custom_common_reading_locked(new_map)
            _CUSTOM_COMMON_READING_DICT = new_map
            _rebuild_common_reading_matcher_locked()
        return w, r

    @classmethod
    def import_custom_common_reading_batch(cls, items: List[Tuple[str, str]]) -> int:
        global _CUSTOM_COMMON_READING_DICT
        ensure_common_reading_dicts_loaded()
        validated: List[Tuple[str, str]] = []
        for raw_w, raw_r in items:
            validated.append(cls.validate_common_reading_entry(raw_w, raw_r))
        if not validated:
            return 0
        with _COMMON_READING_LOCK:
            new_map = dict(_CUSTOM_COMMON_READING_DICT)
            for w, r in validated:
                new_map[w] = r
            cls._save_custom_common_reading_locked(new_map)
            _CUSTOM_COMMON_READING_DICT = new_map
            _rebuild_common_reading_matcher_locked()
        return len(validated)

    @classmethod
    def delete_custom_common_reading(cls, word: str) -> bool:
        global _CUSTOM_COMMON_READING_DICT
        w = _RE_CTRL_CHARS.sub("", (word or "")).strip()
        if not w:
            return False
        ensure_common_reading_dicts_loaded()
        with _COMMON_READING_LOCK:
            if w not in _CUSTOM_COMMON_READING_DICT:
                return False
            new_map = dict(_CUSTOM_COMMON_READING_DICT)
            del new_map[w]
            cls._save_custom_common_reading_locked(new_map)
            _CUSTOM_COMMON_READING_DICT = new_map
            _rebuild_common_reading_matcher_locked()
        return True

    # --- ローカルTTS限定 SudachiPy 安全漢字かな補完 ---

    @classmethod
    def convert_kanji_for_local_tts(cls, text: str) -> str:
        """
        ローカルTTS (VOICEVOX / SHAREVOX / COEIROINK / OpenJTalk) 向けの安全かな補完。
        - クラウドTTS向け原文は変更せず、ローカル合成直前に漢字を含む一般トークンのみをひらがな化する。
        - 助詞（は/へ/を）、数詞＋助数詞、カタカナ/ひらがな、記号、OOV未知語、<<__BEEP:N__>> は元表記を厳格保護する。
        - 漢字を1文字も含まないテキストは 0.001ms 以下のファストパスで即返却する。
        """
        if not text or not _RE_CONTAINS_KANJI.search(text):
            return text

        tokenizer, split_mode = get_sudachi_tokenizer()
        if tokenizer is None or split_mode is None:
            return text

        segments = _RE_BEEP_SPLIT.split(text)
        out_segments: List[str] = []

        for seg in segments:
            if not seg:
                continue
            if _RE_BEEP_TAG.fullmatch(seg) or not _RE_CONTAINS_KANJI.search(seg):
                out_segments.append(seg)
                continue

            try:
                tokens = tokenizer.tokenize(seg, split_mode)
            except Exception as e:
                logger.debug(f"convert_kanji_for_local_tts skipped due to tokenize error: {e}")
                out_segments.append(seg)
                continue

            seg_parts: List[str] = []
            prev_in_numeral_cluster = False

            for tok in tokens:
                surface = tok.surface()
                pos = tok.part_of_speech()
                pos0 = pos[0] if len(pos) > 0 else ""
                pos1 = pos[1] if len(pos) > 1 else ""
                pos2 = pos[2] if len(pos) > 2 else ""

                is_numeral = (
                    (pos1 == "数詞")
                    or bool(_RE_HAS_DIGITS.search(surface))
                    or bool(_RE_KANJI_NUM_ONLY.match(surface))
                )
                is_counter = (pos0 == "接尾辞") or (pos1 == "助数詞") or (pos2 == "助数詞可能")
                in_numeral_context = prev_in_numeral_cluster and is_counter

                should_convert = (
                    not tok.is_oov()
                    and bool(_RE_CONTAINS_KANJI.search(surface))
                    and pos0 not in ("助詞", "補助記号", "記号", "空白")
                    and not is_numeral
                    and not in_numeral_context
                )

                if should_convert:
                    reading = tok.reading_form()
                    if reading and _RE_VALID_KATAKANA_READING.match(reading):
                        seg_parts.append(reading.translate(_KATA_TO_HIRA_TRANS))
                    else:
                        seg_parts.append(surface)
                else:
                    seg_parts.append(surface)

                prev_in_numeral_cluster = is_numeral or in_numeral_context

            out_segments.append("".join(seg_parts))

        return "".join(out_segments)

    @classmethod
    def preview_common_and_local_tts(cls, text: str) -> Dict[str, Any]:
        """共通辞書＋英語辞書＋ローカルTTS漢字かな補完の段階別プレビュー (WebUIテスト用)"""
        src = text or ""
        after_dict = cls.apply_dictionary(src, None)
        after_english, _ = cls.convert_english_to_kana(after_dict)
        after_local_tts = cls.convert_kanji_for_local_tts(after_english)
        return {
            "original": src,
            "after_common_dict": after_dict,
            "after_english_and_dict": after_english,
            "local_tts_kanji_converted": after_local_tts,
        }

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
        読点のない長い文（閾値以上、通常18文字以上、動的短文時は15〜18文字）を、
        超高速・高精度な形態素解析（SudachiPy）を用いて接続詞・接続助詞の自然な境界で分割する。
        SudachiPy が未導入または失敗した場合は Janome / 正規表現ルールに安全フォールバック。
        ※ 14文字以下の短文は形態素解析を完全スキップ（ファストパス）
        """
        raw_text = sentence.strip()
        effective_thresh = max(_FASTPATH_MAX_CHARS + 1, min_chars_threshold)
        if len(raw_text) < effective_thresh or len(_RE_STRIP_PUNCT_SPACES.sub('', raw_text)) <= _FASTPATH_MAX_CHARS:
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
        head_chunk_chars: Optional[int] = None,
    ) -> List['TextChunk']:
        """
        テキストを発言者名プレフィックスおよび多段文境界（句点・読点・スペース・スラング・終助詞・形態素・動的ハードキャップ）で超低遅延パーツ分割する。
        各パーツには音響ストリーミングに最適な間（pause_after_ms）を付与。
        各マシン・各エンジンの実測推論性能統計 (latency_profiler) に基づき、第1チャンク上限 (max_first_chunk_chars) を自律動的決定。
        """
        t_start = time.time()
        _cpu0 = time.thread_time()
        _ru0 = resource.getrusage(resource.RUSAGE_SELF)
        _gc0 = [g["collections"] for g in gc.get_stats()]
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

        # 分割結果が空なら全文を1パーツとして扱う (ただし発音可能文字を1文字も含まない記号列は除外)
        if not chunks and text.strip():
            if '<<__BEEP' in text or '<<ピー音' in text:
                chunks.append(TextChunk(text="", pause_after_ms=0, is_beep=True))
            elif _RE_HAS_READABLE.search(text):
                chunks.append(TextChunk(text=text.strip(), pause_after_ms=0))

        if head_chunk_chars:
            chunks = TextCleaner.apply_head_chunk_limit(chunks, head_chunk_chars)

        split_ms = round((time.time() - t_start) * 1000, 2)
        for c in chunks:
            c.split_ms = split_ms

        if split_ms >= _SPLIT_DIAG_THRESHOLD_MS:
            TextCleaner._log_split_diagnostics(split_ms, len(text), _cpu0, _ru0, _gc0)

        return chunks

    @staticmethod
    def _log_split_diagnostics(split_ms: float, text_len: int, cpu0: float, ru0, gc0) -> None:
        """分割が閾値を超えた時の原因切り分け情報 (CPU時間 vs 壁時計・ページフォールト・GC・稼働スレッド) を記録"""
        try:
            ru1 = resource.getrusage(resource.RUSAGE_SELF)
            cpu_ms = (time.thread_time() - cpu0) * 1000.0
            gc_delta = [b["collections"] - a for a, b in zip(gc0, gc.get_stats())]
            threads = [t.name for t in threading.enumerate()]
            logger.warning(
                f"🔬 [SplitDiag] split={split_ms:.0f}ms cpu={cpu_ms:.0f}ms wait={max(0.0, split_ms - cpu_ms):.0f}ms "
                f"chars={text_len} majflt+={ru1.ru_majflt - ru0.ru_majflt} minflt+={ru1.ru_minflt - ru0.ru_minflt} "
                f"ctxsw(vol/invol)+={ru1.ru_nvcsw - ru0.ru_nvcsw}/{ru1.ru_nivcsw - ru0.ru_nivcsw} "
                f"gc(gen0/1/2)+={gc_delta} threads={len(threads)}:{','.join(threads[:12])}"
            )
        except Exception as e:
            logger.debug(f"split diagnostics failed: {e}")

    @staticmethod
    async def split_into_chunks_async(*args, timeout_s: float = _SPLIT_TIMEOUT_S, **kwargs) -> List['TextChunk']:
        """
        split_into_chunks を専用スレッドで実行し、イベントループを塞がない。
        timeout_s を超えたら形態素解析を使わない正規表現のみの分割で即座に退避する (背景スレッドの結果は破棄)。
        """
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_SPLIT_EXECUTOR, lambda: TextCleaner.split_into_chunks(*args, **kwargs))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout_s)
        except asyncio.TimeoutError:
            fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            logger.warning(f"⏱️ [TextCleaner] 文分割が {timeout_s * 1000:.0f}ms を超過したため、正規表現のみの簡易分割へ退避します。")
            def _fallback_regex_only() -> List['TextChunk']:
                token = _skip_morph.set(True)
                try:
                    return TextCleaner.split_into_chunks(*args, **kwargs)
                finally:
                    _skip_morph.reset(token)
            return await loop.run_in_executor(_SPLIT_FALLBACK_EXECUTOR, _fallback_regex_only)

    @staticmethod
    def apply_head_chunk_limit(chunks: List['TextChunk'], head_chars: int) -> List['TextChunk']:
        """
        先頭本文チャンクだけを head_chars 以内に切り出す (2番目以降の上限は変えない)。
        既存の max_first_chunk_chars は先頭の文全体に効くため、句読点の無い長文で全体が細切れになる。
        こちらは頭の1パーツのみを短くし、残りは独立チャンクとして元の間 (pause) を引き継ぐ。
        """
        head_idx = next(
            (i for i, c in enumerate(chunks)
             if not c.is_name_prefix and not c.is_beep and c.text.strip()),
            None,
        )
        if head_idx is None or len(chunks[head_idx].text) <= head_chars:
            return chunks
        orig = chunks[head_idx]
        pieces = TextCleaner._hardcap_slice(orig.text, max_chars=head_chars)
        head = pieces[0][0] if pieces else ""
        if not head or len(head) >= len(orig.text):
            return chunks
        if orig.text.startswith(head):
            rest = orig.text[len(head):].strip()
        else:
            rest = "".join(p for p, _ in pieces[1:])
        if not rest:
            return chunks
        head_chunk = TextChunk(text=head, pause_after_ms=0)
        rest_chunk = TextChunk(
            text=rest,
            pause_after_ms=orig.pause_after_ms,
            is_followed_by_beep=orig.is_followed_by_beep,
        )
        return chunks[:head_idx] + [head_chunk, rest_chunk] + chunks[head_idx + 1:]

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
            # 14文字以下の短文は形態素解析を完全バイパス (ファストパス: CPU縮退時でも声色維持・遅延ゼロ)
            if raw_len <= _FASTPATH_MAX_CHARS:
                morph_refined.append((sentence, delim))
                continue

            cur_limit = (max_first_chunk_chars if (idx == 0 and is_first_plain_segment and max_first_chunk_chars) else max_chunk_chars)
            if cur_limit < 22:
                # 動的短縮時でも最低15文字以上 (14文字以下は形態素解析を絶対に呼ばない)
                m_thresh = max(_FASTPATH_MAX_CHARS + 1, min(18, int(cur_limit * 0.75)))
                if idx == 0 and is_first_plain_segment and max_first_chunk_chars and max_first_chunk_chars < 18:
                    m_thresh = max(_FASTPATH_MAX_CHARS + 1, min(m_thresh, max_first_chunk_chars))
                m_head = min(7, max(4, m_thresh // 2))
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
        # 先頭チャンクが記号のみの場合は次チャンク先頭へ前方吸着し、全文が記号のみなら空リストを返却する
        cleaned_chunks: List[TextChunk] = []
        pending_symbol_prefix = ""
        for c in chunks:
            if _RE_HAS_READABLE.search(c.text):
                if pending_symbol_prefix:
                    c.text = pending_symbol_prefix + c.text
                    pending_symbol_prefix = ""
                cleaned_chunks.append(c)
            else:
                if cleaned_chunks:
                    cleaned_chunks[-1].text += c.text
                    cleaned_chunks[-1].pause_after_ms = max(cleaned_chunks[-1].pause_after_ms, c.pause_after_ms)
                    cleaned_chunks[-1].is_followed_by_beep = c.is_followed_by_beep
                else:
                    pending_symbol_prefix += c.text

        return cleaned_chunks

    @staticmethod
    def normalize_cache_key_text(text: str) -> str:
        """
        キャッシュキー（SHA-256）計算専用のテキスト正規化。
        音声合成時の感情表現や語尾ゆれ（！、ー、〜、w）を吸収し、キャッシュHIT率を最大化する。
        ※ イントネーションに影響する疑問符「？」および発音モーラ数が異なる語（「おつ」≠「お疲れ」等）は同一視しない。
        """
        if not text:
            return ""
        t = text.strip()

        # 1. 同音のかな/漢字表記ゆれのみ統一 (異音・異モーラ語は含めない)
        greeting_map = {
            "おつかれさまでした": "お疲れ様でした",
            "おつかれさまです": "お疲れ様です",
            "おつかれさま": "お疲れ様",
            "おつかれ": "お疲れ",
            "ありがとうございました": "有難うございました",
            "ありがとうございます": "有難うございます",
            "よろしくおねがいします": "よろしくお願いします",
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
