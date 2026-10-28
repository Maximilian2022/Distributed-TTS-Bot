import asyncio
import time
import aiohttp
import xml.sax.saxutils as saxutils
import logging
from typing import Optional
from .base import BaseTTSClient, RateLimitError, TTSConnectionError, TTSError

logger = logging.getLogger(__name__)

class AzureTTSClient(BaseTTSClient):
    """
    Azure AI Speech REST クライアント (東日本リージョン japaneast)
    aiohttp による非同期ストリーミング接続により、極めて低遅延 (100〜250ms) な音声合成を実現。
    認証エラー(401/403)・レート制限(429)・連続タイムアウト時のサーキットブレーカー搭載。
    """

    def __init__(self, key: str, region: str = "japaneast", timeout_seconds: float = 5.0):
        self.key = key.strip() if key else ""
        self.region = region.strip() if region else "japaneast"
        self.timeout_seconds = timeout_seconds
        self._session: Optional[aiohttp.ClientSession] = None
        self.endpoint = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        self._cooldown_until: float = 0.0
        self._consecutive_failures: int = 0

    @property
    def name(self) -> str:
        return "Azure AI Speech"

    def is_available(self) -> bool:
        return bool(self.key) and (time.time() >= self._cooldown_until)

    def reset_circuit_breaker(self) -> None:
        self._cooldown_until = 0.0
        self._consecutive_failures = 0

    def _record_failure(self, status_code: Optional[int] = None) -> None:
        self._consecutive_failures += 1
        now = time.time()
        if status_code in (401, 403):
            self._cooldown_until = max(self._cooldown_until, now + 180.0)
            logger.warning(f"🛡️ [CircuitBreaker] Azure TTS auth/quota error ({status_code}). Cooling down for 180s.")
        elif status_code == 429:
            self._cooldown_until = max(self._cooldown_until, now + 60.0)
            logger.warning("🛡️ [CircuitBreaker] Azure TTS rate limited (429). Cooling down for 60s.")
        elif self._consecutive_failures >= 2:
            self._cooldown_until = max(self._cooldown_until, now + 45.0)
            logger.warning(
                f"🛡️ [CircuitBreaker] Azure TTS failed {self._consecutive_failures} times consecutively. "
                f"Cooling down for 45s."
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._cooldown_until = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _build_ssml(self, text: str, voice_name: str, pitch: str, speed: float) -> str:
        # XMLエスケープ
        escaped_text = saxutils.escape(text)

        # 速度の表現 (+0%, +20%, -10% など)
        if isinstance(speed, (int, float)):
            rate_percent = f"{(speed - 1.0) * 100:+.0f}%"
        else:
            rate_percent = "0%"

        # ピッチの表現 (例: +0%, +10%, -5%)
        pitch_str = pitch if pitch else "0%"
        if not (pitch_str.endswith("%") or pitch_str.endswith("Hz") or pitch_str.endswith("st")):
            pitch_str = f"{pitch_str}%"

        ssml = (
            f"<speak version='1.0' xml:lang='ja-JP'>"
            f"<voice xml:lang='ja-JP' name='{voice_name}'>"
            f"<prosody rate='{rate_percent}' pitch='{pitch_str}'>"
            f"{escaped_text}"
            f"</prosody>"
            f"</voice>"
            f"</speak>"
        )
        return ssml

    async def synthesize(
        self,
        text: str,
        voice_name: str,
        pitch: str = "0%",
        speed: float = 1.0
    ) -> bytes:
        self.last_worker_label = "Azure Speech (Cloud)"
        if not self.is_available():
            raise TTSConnectionError("Azure Speech API key is not configured.")

        session = await self._get_session()
        ssml = self._build_ssml(text, voice_name, pitch, speed)

        headers = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
            "User-Agent": "DiscordTTSBot-LowLatency"
        }

        try:
            async with session.post(self.endpoint, headers=headers, data=ssml.encode("utf-8")) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) <= 44 or not data.startswith(b"RIFF"):
                        self._record_failure()
                        raise TTSError(f"Azure Speech returned invalid or empty audio ({len(data)} bytes)")
                    self._record_success()
                    return data
                elif resp.status == 429:
                    error_msg = await resp.text()
                    self._record_failure(status_code=429)
                    logger.warning(f"Azure Speech Rate Limit / Quota Exceeded (429): {error_msg}")
                    raise RateLimitError(f"Azure TTS 429 Rate Limit Exceeded: {error_msg}")
                elif resp.status in (401, 403):
                    error_msg = await resp.text()
                    self._record_failure(status_code=resp.status)
                    logger.error(f"Azure Speech Authentication Error ({resp.status}): {error_msg}")
                    raise TTSConnectionError(f"Azure Auth Error ({resp.status}): {error_msg}")
                else:
                    error_msg = await resp.text()
                    self._record_failure(status_code=resp.status)
                    logger.error(f"Azure Speech API Error ({resp.status}): {error_msg}")
                    raise TTSError(f"Azure TTS HTTP {resp.status}: {error_msg}")
        except asyncio.TimeoutError:
            self._record_failure()
            logger.warning(f"Azure Speech request timed out after {self.timeout_seconds}s")
            raise TTSConnectionError(f"Azure Speech Timeout ({self.timeout_seconds}s)")
        except aiohttp.ClientError as e:
            self._record_failure()
            logger.warning(f"Azure Speech network error: {e}")
            raise TTSConnectionError(f"Azure Speech network connection failed: {e}")
