import asyncio
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
    """

    def __init__(self, key: str, region: str = "japaneast", timeout_seconds: float = 5.0):
        self.key = key.strip() if key else ""
        self.region = region.strip() if region else "japaneast"
        self.timeout_seconds = timeout_seconds
        self._session: Optional[aiohttp.ClientSession] = None
        self.endpoint = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"

    @property
    def name(self) -> str:
        return "Azure AI Speech"

    def is_available(self) -> bool:
        return bool(self.key)

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
                        raise TTSError(f"Azure Speech returned invalid or empty audio ({len(data)} bytes)")
                    return data
                elif resp.status == 429:
                    error_msg = await resp.text()
                    logger.warning(f"Azure Speech Rate Limit / Quota Exceeded (429): {error_msg}")
                    raise RateLimitError(f"Azure TTS 429 Rate Limit Exceeded: {error_msg}")
                elif resp.status in (401, 403):
                    error_msg = await resp.text()
                    logger.error(f"Azure Speech Authentication Error ({resp.status}): {error_msg}")
                    raise TTSConnectionError(f"Azure Auth Error ({resp.status}): {error_msg}")
                else:
                    error_msg = await resp.text()
                    logger.error(f"Azure Speech API Error ({resp.status}): {error_msg}")
                    raise TTSError(f"Azure TTS HTTP {resp.status}: {error_msg}")
        except asyncio.TimeoutError:
            logger.warning(f"Azure Speech request timed out after {self.timeout_seconds}s")
            raise TTSConnectionError(f"Azure Speech Timeout ({self.timeout_seconds}s)")
        except aiohttp.ClientError as e:
            logger.warning(f"Azure Speech network error: {e}")
            raise TTSConnectionError(f"Azure Speech network connection failed: {e}")
