import asyncio
import logging
import time
import edge_tts
from src.tts.base import BaseTTSClient, TTSError, TTSConnectionError

logger = logging.getLogger(__name__)

class EdgeTTSClient(BaseTTSClient):
    """
    Microsoft Edge Text-to-Speech (edge-tts) クライアント
    - 完全無料・文字数無制限・APIキー不要
    - クラウド合成のためローカルCPU消費ゼロ
    - 出力形式: 24kHz, 16bit, Mono PCM WAV (他プロバイダーと同一)
    - 要求文字数に応じた動的タイムアウト (4.0s〜9.0s) ＆ 連続失敗時のサーキットブレーカー搭載
    """

    def __init__(self, timeout_seconds: float = 8.0):
        self.timeout_seconds = timeout_seconds
        self._cooldown_until: float = 0.0
        self._consecutive_failures: int = 0

    @property
    def name(self) -> str:
        return "edge"

    def is_available(self) -> bool:
        return time.time() >= self._cooldown_until

    def reset_circuit_breaker(self) -> None:
        self._cooldown_until = 0.0
        self._consecutive_failures = 0

    def calc_dynamic_timeout(self, text_len: int) -> float:
        """
        合成する文字数に応じて適切なタイムアウト秒数を動的に算出する。
        - 短文 (1〜3文字): 最低 4.0s (WSSハンドシェイク + 初回チャンク待機余裕)
        - 中文 (15文字): 約 5.75s
        - 長文 (30文字): 約 8.0s (最大 9.0s キャップ)
        ※ テスト等で timeout_seconds が 4.0s 未満に明示指定されている場合はその値を上限として尊重する。
        """
        if self.timeout_seconds < 4.0:
            return max(0.1, float(self.timeout_seconds))
        n_chars = max(1, int(text_len))
        dyn = 3.5 + (n_chars * 0.15)
        max_cap = max(float(self.timeout_seconds), 9.0)
        return round(min(max_cap, max(4.0, dyn)), 2)

    def _record_failure(self, is_blocked: bool = False) -> None:
        self._consecutive_failures += 1
        now = time.time()
        if is_blocked:
            self._cooldown_until = max(self._cooldown_until, now + 60.0)
            logger.warning("🛡️ [CircuitBreaker] Edge-TTS blocked (403/Handshake). Cooling down for 60s.")
        elif self._consecutive_failures >= 2:
            self._cooldown_until = max(self._cooldown_until, now + 45.0)
            logger.warning(
                f"🛡️ [CircuitBreaker] Edge-TTS failed {self._consecutive_failures} times consecutively. "
                f"Cooling down for 45s."
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._cooldown_until = 0.0

    async def close(self) -> None:
        pass

    SUPPORTED_VOICES = {"ja-JP-NanamiNeural", "ja-JP-KeitaNeural"}
    DEFAULT_FEMALE_VOICE = "ja-JP-NanamiNeural"
    DEFAULT_MALE_VOICE = "ja-JP-KeitaNeural"

    @classmethod
    def normalize_voice_name(cls, voice: str) -> str:
        """
        Edge-TTS で利用可能な正式モデル名 (Nanami / Keita) に正規化・マッピング。
        Azure 等の非対応モデル名が渡された場合もパラメータエラーを防止。
        """
        if not voice:
            return cls.DEFAULT_FEMALE_VOICE
        if voice in cls.SUPPORTED_VOICES:
            return voice
        v_low = voice.lower()
        if any(k in v_low for k in ("keita", "naoki", "daichi", "male", "男")):
            return cls.DEFAULT_MALE_VOICE
        return cls.DEFAULT_FEMALE_VOICE

    @staticmethod
    def _format_rate(speed: float) -> str:
        """speed (1.0基準) を edge-tts の rate 引数 (+0%, +20%, -15%等) に変換"""
        try:
            percent = int(round((float(speed) - 1.0) * 100))
            if percent >= 0:
                return f"+{percent}%"
            return f"{percent}%"
        except Exception:
            return "+0%"

    @staticmethod
    def _format_pitch(pitch: str) -> str:
        """pitch (+10%, -5%等) を edge-tts の pitch 引数 (+10Hz, -5Hz または +10%, -5%) に変換"""
        try:
            cleaned = str(pitch).replace("%", "").replace("+", "").strip()
            val = int(cleaned)
            # Edge-TTSはHzまたは%をサポート
            if val >= 0:
                return f"+{val}Hz"
            return f"{val}Hz"
        except Exception:
            return "+0Hz"

    async def synthesize(
        self,
        text: str,
        voice_name: str,
        pitch: str = "0%",
        speed: float = 1.0
    ) -> bytes:
        """
        edge-tts を用いて音声合成し、FFmpegで 24kHz Mono WAV に変換して返却
        """
        self.last_worker_label = "Edge-TTS (Cloud)"
        if not text.strip():
            raise TTSError("Empty text provided for Edge-TTS synthesis")

        safe_voice = self.normalize_voice_name(voice_name)
        rate_str = self._format_rate(speed)
        pitch_str = self._format_pitch(pitch)
        dyn_timeout = self.calc_dynamic_timeout(len(text))

        try:
            # 1. Edge-TTS から MP3 データを取得 (WSSストリーム自体も動的タイムアウトで保護)
            communicate = edge_tts.Communicate(
                text=text,
                voice=safe_voice,
                rate=rate_str,
                pitch=pitch_str
            )

            async def _collect_mp3() -> bytes:
                chunks = []
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        chunks.append(chunk["data"])
                return b"".join(chunks)

            mp3_bytes = await asyncio.wait_for(_collect_mp3(), timeout=dyn_timeout)
            if not mp3_bytes:
                raise TTSError("Edge-TTS returned empty audio data")

            # 2. FFmpeg でメモリ内 PCM WAV (24kHz, 16bit, 1ch) に変換
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", "pipe:0",
                    "-f", "wav", "-acodec", "pcm_s16le",
                    "-ar", "24000", "-ac", "1",
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                ),
                timeout=dyn_timeout
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=mp3_bytes),
                timeout=dyn_timeout
            )

            if process.returncode != 0:
                err_text = stderr.decode("utf-8", errors="replace")
                logger.error(f"FFmpeg conversion failed: {err_text}")
                raise TTSError(f"FFmpeg conversion failed: {err_text}")

            if len(stdout) < 44 or not stdout.startswith(b"RIFF"):
                raise TTSError(f"Invalid WAV generated by FFmpeg: size={len(stdout)}")

            self._record_success()
            # パイプ出力による 0x7FFFFFFF 未確定ヘッダーサイズを実バイト数で補正
            from src.cache_manager import fix_wav_header
            return fix_wav_header(stdout)

        except asyncio.TimeoutError:
            self._record_failure(is_blocked=False)
            raise TTSConnectionError(f"Edge-TTS synthesis timed out after {dyn_timeout}s (len={len(text)})")
        except Exception as e:
            err_str = str(e)
            is_blocked = ("403" in err_str or "Invalid response status" in err_str)
            self._record_failure(is_blocked=is_blocked)
            if isinstance(e, (TTSError, TTSConnectionError)):
                raise
            if is_blocked:
                logger.warning(f"Edge-TTS 403 Forbidden / API blocked: {err_str} - triggering fallback")
            else:
                logger.error(f"Edge-TTS error: {e}", exc_info=True)
            raise TTSError(f"Edge-TTS synthesis failed: {e}")
