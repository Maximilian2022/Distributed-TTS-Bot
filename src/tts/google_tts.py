import os
import time
import json
import base64
import asyncio
import aiohttp
import logging
from typing import Optional
from .base import BaseTTSClient, RateLimitError, TTSConnectionError, TTSError

try:
    from google.cloud import texttospeech_v1 as texttospeech
    from google.api_core.client_options import ClientOptions
    from google.oauth2 import service_account
    _GCP_SDK_AVAILABLE = True
except ImportError:
    texttospeech = None
    ClientOptions = None
    service_account = None
    _GCP_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

class GoogleTTSClient(BaseTTSClient):
    """
    Google Cloud Text-to-Speech クライアント (東京リージョン asia-northeast1)
    SDK (TextToSpeechAsyncClient) および REST API の両対応で高いフォールバック耐性を提供。
    クォータ超過・認証エラー・連続タイムアウト時のサーキットブレーカー搭載。
    """

    def __init__(
        self,
        credentials_path: str = "",
        api_key: str = "",
        credentials_json: str = "",
        endpoint: str = "asia-northeast1-texttospeech.googleapis.com",
        timeout_seconds: float = 5.0
    ):
        self.credentials_path = credentials_path.strip() if credentials_path else ""
        self.api_key = api_key.strip() if api_key else ""
        self.credentials_json = credentials_json.strip() if credentials_json else ""
        self.endpoint = endpoint.strip() if endpoint else "asia-northeast1-texttospeech.googleapis.com"
        self.timeout_seconds = timeout_seconds
        self._async_client = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._cooldown_until: float = 0.0
        self._consecutive_failures: int = 0
        self._init_sdk()

    def _init_sdk(self) -> None:
        if not _GCP_SDK_AVAILABLE or ClientOptions is None or service_account is None or texttospeech is None:
            return
        try:
            client_options = ClientOptions(api_endpoint=self.endpoint)

            if self.credentials_json:
                info = json.loads(self.credentials_json)
                credentials = service_account.Credentials.from_service_account_info(info)
                self._async_client = texttospeech.TextToSpeechAsyncClient(
                    credentials=credentials,
                    client_options=client_options
                )
            elif self.credentials_path and os.path.exists(self.credentials_path):
                credentials = service_account.Credentials.from_service_account_file(self.credentials_path)
                self._async_client = texttospeech.TextToSpeechAsyncClient(
                    credentials=credentials,
                    client_options=client_options
                )
            elif os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                self._async_client = texttospeech.TextToSpeechAsyncClient(
                    client_options=client_options
                )
        except Exception as e:
            logger.debug(f"Google Cloud TTS SDK async client initialization deferred or skipped: {e}")

    @property
    def name(self) -> str:
        return "Google Cloud Text-to-Speech"

    def is_available(self) -> bool:
        has_creds = bool(
            self._async_client or
            self.api_key or
            self.credentials_path or
            self.credentials_json or
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        )
        return has_creds and (time.time() >= self._cooldown_until)

    def reset_circuit_breaker(self) -> None:
        self._cooldown_until = 0.0
        self._consecutive_failures = 0

    def _record_failure(self, is_quota: bool = False, is_auth: bool = False) -> None:
        self._consecutive_failures += 1
        now = time.time()
        if is_auth:
            self._cooldown_until = max(self._cooldown_until, now + 180.0)
            logger.warning("🛡️ [CircuitBreaker] Google Cloud TTS auth error. Cooling down for 180s.")
        elif is_quota:
            self._cooldown_until = max(self._cooldown_until, now + 120.0)
            logger.warning("🛡️ [CircuitBreaker] Google Cloud TTS quota/rate limit (429). Cooling down for 120s.")
        elif self._consecutive_failures >= 2:
            self._cooldown_until = max(self._cooldown_until, now + 45.0)
            logger.warning(
                f"🛡️ [CircuitBreaker] Google Cloud TTS failed {self._consecutive_failures} times consecutively. "
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

    def _convert_pitch_to_semitones(self, pitch_str: str) -> float:
        """Azure等のパーセント表記 (+10%, -5%) をGoogleのセミトーン値 (-20.0 〜 20.0) に概算変換"""
        try:
            cleaned = pitch_str.replace("%", "").strip()
            val = float(cleaned)
            # 10% 約 +1.0 semitone
            semitones = val / 10.0
            return max(-20.0, min(20.0, semitones))
        except (ValueError, TypeError):
            return 0.0

    async def synthesize(
        self,
        text: str,
        voice_name: str,
        pitch: str = "0%",
        speed: float = 1.0
    ) -> bytes:
        self.last_worker_label = "Google Cloud (Cloud)"
        if not self.is_available():
            raise TTSConnectionError("Google Cloud TTS credentials / API key not configured.")

        # 1. SDK が利用可能な場合は SDK で非同期リクエスト
        if self._async_client and texttospeech is not None:
            try:
                pitch_semitones = self._convert_pitch_to_semitones(pitch)
                speaking_rate = max(0.25, min(4.0, float(speed)))

                # 実在しないja-JP-Neural2-F等の安全補正ガード
                if voice_name == "ja-JP-Neural2-F":
                    voice_name = "ja-JP-Neural2-B"

                synthesis_input = texttospeech.SynthesisInput(text=text)
                voice = texttospeech.VoiceSelectionParams(
                    language_code="ja-JP",
                    name=voice_name
                )
                audio_config = texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                    speaking_rate=speaking_rate,
                    pitch=pitch_semitones,
                    sample_rate_hertz=24000
                )

                response = await asyncio.wait_for(
                    self._async_client.synthesize_speech(
                        input=synthesis_input,
                        voice=voice,
                        audio_config=audio_config
                    ),
                    timeout=self.timeout_seconds
                )
                self._record_success()
                return response.audio_content
            except asyncio.TimeoutError:
                self._record_failure()
                raise TTSConnectionError(f"Google Cloud TTS SDK request timed out after {self.timeout_seconds}s")
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "ResourceExhausted" in err_str or "Quota" in err_str:
                    self._record_failure(is_quota=True)
                    raise RateLimitError(f"Google Cloud TTS quota exceeded: {err_str}")
                if not self.api_key:
                    is_auth = any(k in err_str for k in ("401", "403", "Unauthenticated", "PermissionDenied"))
                    self._record_failure(is_auth=is_auth)
                logger.warning(f"Google Cloud TTS SDK error, attempting REST fallback if available: {e}")

        # 2. REST API (APIキーがある場合やSDKフォールバック)
        if self.api_key:
            return await self._synthesize_rest(text, voice_name, pitch, speed)

        raise TTSError("Google Cloud TTS synthesis failed and no secondary fallback available.")

    async def _synthesize_rest(
        self,
        text: str,
        voice_name: str,
        pitch: str,
        speed: float
    ) -> bytes:
        session = await self._get_session()
        url = f"https://{self.endpoint}/v1/text:synthesize?key={self.api_key}"

        pitch_semitones = self._convert_pitch_to_semitones(pitch)
        speaking_rate = max(0.25, min(4.0, float(speed)))

        # 実在しないja-JP-Neural2-F等の安全補正ガード
        if voice_name == "ja-JP-Neural2-F":
            voice_name = "ja-JP-Neural2-B"

        payload = {
            "input": {"text": text},
            "voice": {
                "languageCode": "ja-JP",
                "name": voice_name
            },
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": 24000,
                "speakingRate": speaking_rate,
                "pitch": pitch_semitones
            }
        }

        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    audio_content_b64 = data.get("audioContent", "")
                    decoded = base64.b64decode(audio_content_b64)
                    self._record_success()
                    return decoded
                elif resp.status == 429:
                    text_err = await resp.text()
                    self._record_failure(is_quota=True)
                    raise RateLimitError(f"Google TTS 429 Rate Limit: {text_err}")
                elif resp.status in (401, 403):
                    text_err = await resp.text()
                    self._record_failure(is_auth=True)
                    raise TTSConnectionError(f"Google TTS Auth Error ({resp.status}): {text_err}")
                else:
                    text_err = await resp.text()
                    self._record_failure()
                    raise TTSError(f"Google TTS HTTP {resp.status}: {text_err}")
        except asyncio.TimeoutError:
            self._record_failure()
            raise TTSConnectionError(f"Google TTS REST timed out ({self.timeout_seconds}s)")
        except aiohttp.ClientError as e:
            self._record_failure()
            raise TTSConnectionError(f"Google TTS REST network error: {e}")
