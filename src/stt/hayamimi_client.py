import asyncio
import json
import logging
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple
import aiohttp

logger = logging.getLogger(__name__)

# 早耳 Live / Hayamimi 対応翻訳言語リスト (コード, 表示名)
HAYAMIMI_TRANSLATE_LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("en", "英語 (English / en)"),
    ("zh", "中国語・簡体字 (Chinese / zh)"),
    ("zh-tw", "台湾語・中国語繁体字 (Traditional Chinese / zh-tw)"),
    ("ko", "韓国語 (Korean / ko)"),
    ("es", "スペイン語 (Spanish / es)"),
    ("fr", "フランス語 (French / fr)"),
    ("de", "ドイツ語 (German / de)"),
    ("pt", "ポルトガル語 (Portuguese / pt)"),
)
HAYAMIMI_LANGUAGE_LABELS: Dict[str, str] = {
    code: label for code, label in HAYAMIMI_TRANSLATE_LANGUAGES
}
HAYAMIMI_VALID_LANG_CODES: FrozenSet[str] = frozenset(HAYAMIMI_LANGUAGE_LABELS.keys())

_RE_TRANS_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_RE_TRANS_USER_MENTION = re.compile(r"<@!?(\d+)>")
_RE_TRANS_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_RE_TRANS_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
_RE_TRANS_CUSTOM_EMOJI = re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")
_RE_TRANS_SPACES = re.compile(r"[ \t]+")
_RE_TRANS_MULTI_NEWLINES = re.compile(r"\n{3,}")
_RE_HAS_JP_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\uff66-\uff9f]")
_RE_ONLY_W_LAUGH = re.compile(r"^[wWｗ\s!！?？。、.~〜ー]+$")
_RE_ONLY_KANJI_AND_PUNCT = re.compile(r"^[\u4e00-\u9faf\s、。！？!?,.\-〜ー]+$")
_RE_KANJI_CHAR = re.compile(r"[\u4e00-\u9faf]")


def normalize_hayamimi_lang(code: Optional[str], default: str = "en") -> str:
    """Hayamimi 翻訳先言語コードを正規化 (不正値時はデフォルトへフォールバック)"""
    val = str(code or "").strip().lower()
    if val in HAYAMIMI_VALID_LANG_CODES:
        return val
    return default


def resolve_stt_trans_langs(
    trans_lang_1: Optional[str],
    trans_lang_2: Optional[str] = None,
    default_trans_langs: str = "",
) -> str:
    """
    /stt start の選択肢 (trans_lang_1, trans_lang_2) と既定値から trans_langs を構築する。
    - 両方 None の場合のみ default_trans_langs をフォールバック使用する。
    - "none" / 空文字は除外し、順序を保持して重複排除したカンマ区切り文字列を返す。
    """
    if trans_lang_1 is None and trans_lang_2 is None:
        return (default_trans_langs or "").strip()

    langs: List[str] = []
    for raw in (trans_lang_1, trans_lang_2):
        if raw is None:
            continue
        code = str(raw).strip().lower()
        if not code or code == "none":
            continue
        if code not in langs:
            langs.append(code)
    return ",".join(langs)


def resolve_hayamimi_translate_url(gateway_url: Optional[str]) -> str:
    """
    WebSocket Gateway URL (ws://... または wss://...) から
    テキスト双方向翻訳 API エンドポイント (.../api/translate) を導出する。
    """
    raw = str(gateway_url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    if not parsed.netloc:
        return ""
    scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(parsed.scheme.lower())
    if not scheme:
        return ""
    return urllib.parse.urlunparse((scheme, parsed.netloc, "/api/translate", "", "", ""))


def prepare_text_for_translation(raw_text: str, guild: Any = None) -> Optional[str]:
    """
    生の Discord メッセージテキストを翻訳用に軽量整形する。
    - メンション (<@ID>, <@&ID>, <#ID>) を表示名へ展開
    - URL およびカスタム絵文字タグを除去
    - URLのみ・絵文字のみ・記号/数字のみで自然言語文字を含まない場合は None を返す
    """
    if not raw_text:
        return None
    text = raw_text

    if guild is not None:
        def _rep_user(m: re.Match) -> str:
            uid = int(m.group(1))
            member = guild.get_member(uid) if hasattr(guild, "get_member") else None
            return f"@{member.display_name}" if member else ""

        def _rep_role(m: re.Match) -> str:
            rid = int(m.group(1))
            role = guild.get_role(rid) if hasattr(guild, "get_role") else None
            return f"@{role.name}" if role else ""

        def _rep_channel(m: re.Match) -> str:
            cid = int(m.group(1))
            ch = guild.get_channel(cid) if hasattr(guild, "get_channel") else None
            return f"#{ch.name}" if ch else ""

        text = _RE_TRANS_USER_MENTION.sub(_rep_user, text)
        text = _RE_TRANS_ROLE_MENTION.sub(_rep_role, text)
        text = _RE_TRANS_CHANNEL_MENTION.sub(_rep_channel, text)
    else:
        text = _RE_TRANS_USER_MENTION.sub("", text)
        text = _RE_TRANS_ROLE_MENTION.sub("", text)
        text = _RE_TRANS_CHANNEL_MENTION.sub("", text)

    text = _RE_TRANS_URL.sub("", text)
    text = _RE_TRANS_CUSTOM_EMOJI.sub("", text)
    text = _RE_TRANS_SPACES.sub(" ", text)
    text = _RE_TRANS_MULTI_NEWLINES.sub("\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if not text:
        return None

    non_emoji = text
    try:
        import emoji as _emoji_mod
        if hasattr(_emoji_mod, "replace_emoji"):
            non_emoji = _emoji_mod.replace_emoji(text, replace="")
    except Exception:
        pass

    if not any(ch.isalpha() for ch in non_emoji):
        return None

    return text


def should_translate_for_japanese_tts(
    prepared_text: Optional[str],
    dictionary_entries: Optional[List[Tuple[str, str]]] = None,
) -> bool:
    """
    翻訳オプション ON のチャンネルにおいて、テキストを Hayamimi へ送って
    日本語訳読み上げを試みるべきかを高速判定する (正規表現によるサブミリ秒判定)。
    - ひらがな・カタカナを含む文、w のみの笑い、辞書登録語完全一致、1〜2文字の漢字熟語は False (0ms即時日本語ルート)
    - 英語・韓国語・3文字以上の中国語等、非日本語の可能性がある文は True
    """
    if not prepared_text:
        return False
    stripped = prepared_text.strip()
    if not stripped:
        return False

    # 1. ひらがな・カタカナが1文字でも含まれれば日本語ベース
    if _RE_HAS_JP_KANA.search(stripped):
        return False

    # 2. w / ｗ のみの笑い表現は翻訳しない
    if _RE_ONLY_W_LAUGH.match(stripped):
        return False

    # 3. サーバー単語辞書に完全一致する語は既存の読み上げを優先
    if dictionary_entries:
        lowered = stripped.lower()
        for word, _ in dictionary_entries:
            if word and str(word).strip().lower() == lowered:
                return False

    # 4. 漢字と記号のみで漢字が 1〜2文字以下 (例:「了解」「開始」) は誤判定防止のため日本語扱い
    if _RE_ONLY_KANJI_AND_PUNCT.match(stripped):
        if len(_RE_KANJI_CHAR.findall(stripped)) <= 2:
            return False

    # 5. 自然言語の文字が1文字以上あれば翻訳判定対象
    return any(ch.isalpha() for ch in stripped)


def extract_translation_output(
    res: Optional[Dict[str, Any]],
    original_text: str,
    target_lang: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Hayamimi POST /api/translate レスポンスから (source_lang, out_lang, translated_text) を抽出する。
    無効・空・原文と同一の場合は translated_text を None にする。
    """
    if not res or not res.get("ok"):
        return None, None, None

    translations = res.get("translations")
    if not isinstance(translations, dict) or not translations:
        return None, None, None

    source_lang = str(res.get("source_lang") or "").strip().lower()
    if source_lang == "ja":
        out_lang = target_lang
        translated_raw = translations.get(target_lang)
        if translated_raw is None:
            for k, v in translations.items():
                if str(k).strip().lower() == target_lang.lower():
                    translated_raw = v
                    break
    else:
        out_lang = "ja" if "ja" in translations else str(next(iter(translations.keys()))).lower()
        translated_raw = translations.get(out_lang)

    if not translated_raw:
        return source_lang, out_lang, None

    translated_str = str(translated_raw).strip()
    if not translated_str or translated_str == original_text.strip() or source_lang == out_lang:
        return source_lang, out_lang, None

    return source_lang, out_lang, translated_str


class HayamimiTranslator:
    """早耳 Live テキスト双方向翻訳 API (POST /api/translate) 用永続非同期クライアント"""

    def __init__(self, config: Any):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock: Optional[asyncio.Lock] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10.0, connect=3.0)
                )
            return self._session

    async def translate(
        self,
        text: str,
        target_langs: Optional[List[str]] = None,
        source_lang: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict[str, Any]]:
        """
        テキストを Hayamimi POST /api/translate へ送信し、結果 JSON を返す。
        接続不能・未設定・タイムアウト時は例外を投げず None を返す。
        """
        translate_url = resolve_hayamimi_translate_url(getattr(self.config, "hayamimi_ws_url", ""))
        if not translate_url or not text or not text.strip():
            return None

        payload: Dict[str, Any] = {"text": text}
        if target_langs:
            payload["target_langs"] = list(target_langs)
        if source_lang:
            payload["source_lang"] = source_lang

        try:
            session = await self._get_session()
            async with session.post(
                translate_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout, connect=3.0),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        f"⚠️ [Hayamimi Translate] HTTP {resp.status} from {translate_url}: {body[:200]}"
                    )
                    return None
                data = await resp.json()
                if not isinstance(data, dict) or not data.get("ok"):
                    logger.warning(f"⚠️ [Hayamimi Translate] Unexpected response: {data}")
                    return None
                return data
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"⚠️ [Hayamimi Translate] Request failed ({translate_url}): {e}")
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


class HayamimiSession:
    """早耳 Live (Hayamimi) との 1 話者分の WebSocket ストリーミングセッション"""

    def __init__(
        self,
        user_id: int,
        user_name: str,
        gateway_url: str,
        model: str = "reazon",
        trans_langs: str = "",
        gemini_key: str = "",
        token: str = "",
        on_transcript: Optional[Callable[[int, str, Dict[str, Any]], Any]] = None,
        on_translation: Optional[Callable[[int, str, Dict[str, Any]], Any]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self.user_id = user_id
        self.user_name = user_name
        self.gateway_url = gateway_url
        self.model = model or "reazon"
        self.trans_langs = trans_langs or ""
        self.gemini_key = gemini_key or ""
        self.token = token or ""
        self.on_transcript = on_transcript
        self.on_translation = on_translation
        self._loop = loop

        self.session_id: Optional[str] = None
        self.is_connected = False
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=150)
        self._recv_task: Optional[asyncio.Task] = None
        self._send_task: Optional[asyncio.Task] = None
        self._closed = False

    def build_ws_url(self) -> str:
        """接続用 WebSocket URL を生成"""
        params = {
            "sample_rate": 48000,
            "channels": 2,
            "format": "s16le",
            "model": self.model,
        }
        if self.trans_langs:
            params["trans_langs"] = self.trans_langs
        if self.gemini_key:
            params["gemini_key"] = self.gemini_key
        if self.token:
            params["token"] = self.token

        query_str = urllib.parse.urlencode(params)
        delimiter = "&" if "?" in self.gateway_url else "?"
        return f"{self.gateway_url}{delimiter}{query_str}"

    async def start(self) -> bool:
        """WebSocket 接続を開始し、ready 受信後に送受信タスクを立ち上げる"""
        if self._closed:
            return False

        if not self._loop:
            self._loop = asyncio.get_running_loop()

        target_url = self.build_ws_url()
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=5.0)
            )
            self._ws = await self._session.ws_connect(target_url, heartbeat=15.0)

            # サーバーからの ready メッセージを待機 (初期化完了を確実に保証)
            ready_msg = await self._ws.receive()
            if ready_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(ready_msg.data)
                    if data.get("type") == "ready":
                        self.session_id = data.get("session_id")
                        logger.info(f"✨ [Hayamimi] Session ready: {self.session_id} (user: {self.user_name})")
                except Exception:
                    pass

            self.is_connected = True

            self._recv_task = asyncio.create_task(self._recv_loop(), name=f"hayamimi_recv_{self.user_id}")
            self._send_task = asyncio.create_task(self._send_loop(), name=f"hayamimi_send_{self.user_id}")
            logger.info(f"🎙️ [Hayamimi] Connected session for user {self.user_name} ({self.user_id})")
            return True
        except Exception as e:
            logger.error(f"❌ [Hayamimi] Failed to connect for user {self.user_id}: {e}")
            await self.close()
            return False

    def push_audio(self, pcm_chunk: bytes) -> None:
        """48kHz Stereo 16-bit PCM (20ms = 3840 bytes) を送信キューに投入 (スレッドセーフ)"""
        if self._closed or not self.is_connected or not self._loop:
            return

        def _do_put():
            try:
                self._send_queue.put_nowait(pcm_chunk)
            except asyncio.QueueFull:
                try:
                    self._send_queue.get_nowait()
                    self._send_queue.put_nowait(pcm_chunk)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        try:
            self._loop.call_soon_threadsafe(_do_put)
        except RuntimeError:
            pass

    async def _send_loop(self) -> None:
        """PCM バイナリフレームを順次送信"""
        sent_bytes = 0
        sent_chunks = 0
        last_log_time = time.time()
        try:
            while not self._closed and self._ws and not self._ws.closed:
                chunk = await self._send_queue.get()
                if self._ws and not self._ws.closed:
                    await self._ws.send_bytes(chunk)
                    sent_bytes += len(chunk)
                    sent_chunks += 1
                    now = time.time()
                    if now - last_log_time >= 5.0 and sent_chunks > 0:
                        last_log_time = now
                        logger.debug(
                            f"📡 [Hayamimi Send] {self.user_name} ({self.user_id}): "
                            f"{sent_chunks} chunks ({sent_bytes / 1024:.1f} KB) sent to session {self.session_id}"
                        )
                self._send_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._closed:
                logger.warning(f"⚠️ [Hayamimi] Send error for user {self.user_id}: {e}")

    async def _recv_loop(self) -> None:
        """早耳 Live からの JSON イベント (ready, transcript, translation, error) を受信"""
        try:
            while not self._closed and self._ws and not self._ws.closed:
                msg = await self._ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ [Hayamimi WS] Non-JSON text received: {msg.data[:100]}")
                        continue

                    msg_type = data.get("type")
                    if msg_type == "ready":
                        self.session_id = data.get("session_id")
                        logger.info(f"✨ [Hayamimi] Session ready: {self.session_id} (user: {self.user_name})")
                    elif msg_type == "transcript":
                        text = data.get("text", "")
                        is_final = data.get("is_final", False)
                        seq = data.get("seq")
                        lat = data.get("latency_ms")
                        logger.info(
                            f"✨ [Hayamimi WS] Transcript: user='{self.user_name}' (ID: {self.user_id}) | "
                            f"final={is_final} | seq={seq} | latency={lat}ms | text='{text}'"
                        )
                        if self.on_transcript:
                            try:
                                res = self.on_transcript(self.user_id, self.user_name, data)
                                if asyncio.iscoroutine(res):
                                    asyncio.create_task(res)
                            except Exception as ex:
                                logger.exception(f"Error in on_transcript callback: {ex}")
                    elif msg_type == "translation":
                        lang = data.get("lang")
                        trans_text = data.get("text")
                        logger.info(
                            f"🌐 [Hayamimi WS] Translation: user='{self.user_name}' | "
                            f"lang={lang} | text='{trans_text}'"
                        )
                        if self.on_translation:
                            try:
                                res = self.on_translation(self.user_id, self.user_name, data)
                                if asyncio.iscoroutine(res):
                                    asyncio.create_task(res)
                            except Exception as ex:
                                logger.exception(f"Error in on_translation callback: {ex}")
                    elif msg_type == "error":
                        logger.warning(f"⚠️ [Hayamimi WS] Server error: {data.get('message')} (user: {self.user_name})")
                    elif msg_type == "ping":
                        await self._ws.send_str(json.dumps({"type": "pong", "ts": time.time()}))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._closed:
                logger.warning(f"⚠️ [Hayamimi] Receive error for user {self.user_id}: {e}")
        finally:
            if not self._closed:
                await self.close()

    async def close(self) -> None:
        """セッションの正常終了"""
        self._closed = True
        self.is_connected = False
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.debug(f"🛑 [Hayamimi] Session closed for user {self.user_id}")

