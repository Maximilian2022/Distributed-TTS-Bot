import os
import time
import aiohttp
import asyncio
import logging
from typing import Optional, Dict, Any, List, Tuple
from src.tts.base import BaseTTSClient, TTSError, TTSConnectionError
from src.tts.node_pool import TTSNodePool

logger = logging.getLogger(__name__)


def build_coeiroink_warmup_payload(voice_name: str, text: str = "っ") -> Optional[Dict[str, Any]]:
    """
    COEIROINK のウォームアップ / 事前ロード用ダミー推論 payload を生成
    voice_name: 例 "speakerUuid_styleId"
    """
    parts = str(voice_name).split("_")
    if len(parts) < 2:
        return None
    spk_uuid = parts[0]
    style_id = int(parts[1]) if parts[1].isdigit() else 0
    return {
        "speakerUuid": spk_uuid,
        "styleId": style_id,
        "text": text,
        "speedScale": 1.0,
        "pitchScale": 0.0,
        "volumeScale": 1.0,
        "intonationScale": 1.0,
        "prePhonemeLength": 0.05,
        "postPhonemeLength": 0.05,
        "outputSamplingRate": 24000
    }


class CoeiroinkTTSClient(BaseTTSClient):
    """
    COEIROINK Engine (Docker REST API v1) を用いたローカル/分散音声合成クライアント
    - 単一ノード (base_url) およびマルチノードプール (node_pool) に対応
    - 出力形式: 24kHz, 16bit, Mono PCM WAV (Cloud TTS / VOICEVOXと完全互換)
    - /v1/speakers から全キャラクター・全スタイル情報を動的取得
    """

    DEFAULT_SPEAKER_UUID = "3c37646f-3881-5374-2a83-149267990abc"  # つくよみちゃん

    def __init__(
        self,
        base_url: str = "http://coeiroink:50031",
        timeout_seconds: float = 15.0,
        node_pool: Optional[TTSNodePool] = None
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.node_pool = node_pool
        self._session: Optional[aiohttp.ClientSession] = None
        self._speakers_cache: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "coeiroink"

    def is_available(self) -> bool:
        if self.node_pool:
            return any(n.is_available for n in self.node_pool.get_nodes())
        return bool(self.base_url)

    async def _get_session(self) -> aiohttp.ClientSession:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if (
            self._session is None
            or self._session.closed
            or (curr_loop is not None and getattr(self._session, "_loop", None) is not curr_loop)
        ):
            if self._session and not self._session.closed:
                try:
                    await self._session.close()
                except Exception:
                    pass
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _convert_pitch_to_scale(pitch_str: str) -> float:
        """Azure等のパーセント表記 (+10%, -5%) を COEIROINK の pitchScale (-0.15 〜 0.15) に変換"""
        try:
            cleaned = pitch_str.replace("%", "").replace("+", "").strip()
            val = float(cleaned)
            return max(-0.15, min(0.15, val / 100.0))
        except (ValueError, TypeError):
            return 0.0

    def _parse_voice_identifier(self, voice_name: str) -> Tuple[str, int]:
        """
        voice_name から (speaker_uuid, style_id) を解決
        形式例:
        - "ci_3c37646f-3881-5374-2a83-149267990abc_0" -> (uuid, 0)
        - "3c37646f-3881-5374-2a83-149267990abc_0" -> (uuid, 0)
        - "ci_0" -> (DEFAULT_SPEAKER_UUID, 0)
        - "0" -> (DEFAULT_SPEAKER_UUID, 0)
        """
        v = str(voice_name)
        if v.startswith("ci_"):
            v = v[3:]

        if "_" in v:
            parts = v.split("_")
            uuid = parts[0]
            try:
                style_id = int(parts[1])
            except ValueError:
                style_id = 0
            return uuid, style_id
        else:
            try:
                style_id = int(v)
            except ValueError:
                style_id = 0
            return self.DEFAULT_SPEAKER_UUID, style_id

    async def get_speakers(self) -> List[Dict[str, Any]]:
        """
        COEIROINK Engine から全キャラクター・全スタイル情報を取得
        """
        base_url = self.node_pool.get_primary_url() if self.node_pool else self.base_url
        if not base_url:
            return []
        session = await self._get_session()
        url = f"{base_url}/v1/speakers"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._speakers_cache = data
                    return data
                else:
                    err = await resp.text()
                    logger.error(f"COEIROINK /v1/speakers failed ({resp.status}): {err}")
                    return []
        except Exception as e:
            logger.debug(f"COEIROINK engine unreachable from {url} (container may be idle): {e}")
            return []

    async def synthesize(
        self,
        text: str,
        voice_name: str,
        pitch: str = "0%",
        speed: float = 1.0,
        is_longest_chunk: bool = False
    ) -> bytes:
        """
        指定されたテキストと話者・スタイルIDでCOEIROINK音声合成を実行
        ノードプールが設定されている場合は利用可能なノードを自動選択して分散実行
        """
        t0 = time.time()
        preview_text = text[:30] + ("..." if len(text) > 30 else "")
        if self.node_pool:
            lease = await self.node_pool.acquire_node(
                timeout=self.timeout_seconds,
                text_len=len(text),
                is_longest_chunk=is_longest_chunk,
                model_name=voice_name
            )
            if not lease:
                raise TTSConnectionError("No available COEIROINK node in pool (all busy or offline)")
            async with lease as node:
                try:
                    # 外部ワーカーへのリクエストは最大12.0秒（Speculative TTSと調和させ、誤隔離を完全防止）
                    default_ext_timeout = min(self.timeout_seconds, 12.0)
                    worker_timeout = float(os.getenv("EXTERNAL_WORKER_TIMEOUT_SECONDS", str(default_ext_timeout))) if node.is_external else self.timeout_seconds
                    if getattr(node, "is_ws", False):
                        res = await node.synthesize(
                            engine="coeiroink",
                            text=text,
                            voice_name=voice_name,
                            pitch=pitch,
                            speed=speed,
                            timeout_seconds=worker_timeout
                        )
                    else:
                        res = await self._synthesize_on_node(
                            node.url, text, voice_name, pitch, speed, timeout_seconds=worker_timeout
                        )
                    latency_ms = (time.time() - t0) * 1000.0
                    self.last_worker_label = node.summary_label
                    logger.info(
                        f"🎙️ [COEIROINK] 音声合成完了 ({latency_ms:.0f}ms): worker={node.summary_label} | text='{preview_text}'"
                    )
                    return res
                except Exception as e:
                    # 外部Workerノードでの失敗時は、NodeLease.__aexit__ で自動的に mark_failed が実行される。
                    # Masterローカルの起動状態確認やフォールバックは TTSManager 側のオーケストレーターに一任し、
                    # 未起動コンテナへの不用意なリクエストによる DNS解決エラー (Name or service not known) を防止。
                    logger.warning(
                        f"⚠️ [COEIROINK] Worker '{node.id}' で合成失敗 ({e})。TTSManager のフォールバックチェーンへ移行します。"
                    )
                    raise
        else:
            # 単一ノード時
            res = await self._synthesize_on_node(self.base_url, text, voice_name, pitch, speed)
            latency_ms = (time.time() - t0) * 1000.0
            self.last_worker_label = "local_master (CPU)"
            logger.info(
                f"🎙️ [COEIROINK] 音声合成完了 ({latency_ms:.0f}ms): worker='local_master' (Masterローカル) | text='{preview_text}'"
            )
            return res

    async def _synthesize_on_node(
        self,
        target_url: str,
        text: str,
        voice_name: str,
        pitch: str = "0%",
        speed: float = 1.0,
        timeout_seconds: Optional[float] = None
    ) -> bytes:
        session = await self._get_session()
        speaker_uuid, style_id = self._parse_voice_identifier(voice_name)

        pitch_scale = self._convert_pitch_to_scale(pitch)
        speaking_rate = max(0.5, min(2.0, float(speed)))

        payload = {
            "speakerUuid": speaker_uuid,
            "styleId": style_id,
            "text": text,
            "speedScale": speaking_rate,
            "pitchScale": pitch_scale,
            "volumeScale": 1.0,
            "intonationScale": 1.0,
            "prePhonemeLength": 0.1,
            "postPhonemeLength": 0.1,
            "outputSamplingRate": 24000
        }

        synthesis_url = f"{target_url}/v1/synthesis"
        headers = {
            "Content-Type": "application/json",
            "accept": "audio/wav"
        }

        eff_timeout = timeout_seconds or self.timeout_seconds
        req_timeout = aiohttp.ClientTimeout(total=eff_timeout)

        try:
            async with session.post(synthesis_url, json=payload, headers=headers, timeout=req_timeout) as resp:
                if resp.status == 200:
                    return await resp.read()
                else:
                    err_msg = await resp.text()
                    logger.error(f"COEIROINK synthesis failed on {target_url} ({resp.status}): {err_msg}")
                    raise TTSError(f"COEIROINK synthesis HTTP {resp.status}: {err_msg}")
        except asyncio.TimeoutError:
            raise TTSConnectionError(f"COEIROINK synthesis timed out on {target_url} after {eff_timeout:.1f}s")
        except aiohttp.ClientError as e:
            raise TTSConnectionError(f"COEIROINK network error during synthesis on {target_url}: {e}")
