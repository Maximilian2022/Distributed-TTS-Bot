import os
import time
import aiohttp
import asyncio
import logging
import urllib.parse
from typing import Optional, Dict, Any, List
from src.tts.base import BaseTTSClient, TTSError, TTSConnectionError
from src.tts.node_pool import TTSNodePool

logger = logging.getLogger(__name__)

class SharevoxTTSClient(BaseTTSClient):
    """
    SHAREVOX Engine (Docker REST API) を用いたローカル/分散音声合成クライアント
    - 単一ノード (base_url) およびマルチノードプール (node_pool) に対応
    - 出力形式: 24kHz, 16bit, Mono PCM WAV (Cloud TTS / VOICEVOXと完全互換)
    - /speakers から全キャラクター・全スタイル情報を動的取得
    """

    def __init__(
        self,
        base_url: str = "http://sharevox:50025",
        timeout_seconds: float = 10.0,
        node_pool: Optional[TTSNodePool] = None
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.node_pool = node_pool
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def name(self) -> str:
        return "sharevox"

    def is_available(self) -> bool:
        if self.node_pool:
            return len(self.node_pool.get_nodes()) > 0
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
        """Azure等のパーセント表記 (+10%, -5%) を SHAREVOX の pitchScale (-0.15 〜 0.15) に変換"""
        try:
            cleaned = pitch_str.replace("%", "").replace("+", "").strip()
            val = float(cleaned)
            return max(-0.15, min(0.15, val / 100.0))
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _extract_style_id(voice_name: str) -> int:
        """voice_name (例: 'sv_14', '14') から style_id (int) を抽出"""
        try:
            if str(voice_name).startswith("sv_"):
                return int(str(voice_name).replace("sv_", ""))
            return int(voice_name)
        except (ValueError, TypeError):
            return 14  # デフォルト: 小春音アミ (ノーマルv3)

    async def get_speakers(self) -> List[Dict[str, Any]]:
        """
        SHAREVOX Engine から全キャラクター・全スタイル情報を取得
        """
        base_url = self.node_pool.get_primary_url() if self.node_pool else self.base_url
        if not base_url:
            return []
        session = await self._get_session()
        url = f"{base_url}/speakers"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    err = await resp.text()
                    logger.error(f"SHAREVOX /speakers failed ({resp.status}): {err}")
                    return []
        except Exception as e:
            logger.debug(f"SHAREVOX engine unreachable from {url} (container may be idle): {e}")
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
        指定されたテキストとスタイルIDでSHAREVOX音声合成を実行
        ノードプールが設定されている場合は利用可能なノードを自動選択して分散実行
        """
        t0 = time.time()
        preview_text = text[:30] + ("..." if len(text) > 30 else "")
        if self.node_pool:
            lease = await self.node_pool.acquire_node(
                timeout=self.timeout_seconds,
                text_len=len(text),
                is_longest_chunk=is_longest_chunk
            )
            if not lease:
                raise TTSConnectionError("No available SHAREVOX node in pool (all busy or offline)")
            async with lease as node:
                worker_timeout = float(os.getenv("EXTERNAL_WORKER_TIMEOUT_SECONDS", str(self.timeout_seconds))) if node.is_external else self.timeout_seconds
                if getattr(node, "is_ws", False):
                    res = await node.synthesize(
                        engine="sharevox",
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
                    f"🎙️ [SHAREVOX] 音声合成完了 ({latency_ms:.0f}ms): worker={node.summary_label} | text='{preview_text}'"
                )
                return res
        else:
            res = await self._synthesize_on_node(self.base_url, text, voice_name, pitch, speed)
            latency_ms = (time.time() - t0) * 1000.0
            self.last_worker_label = "local_master (CPU)"
            logger.info(
                f"🎙️ [SHAREVOX] 音声合成完了 ({latency_ms:.0f}ms): worker='local_master' (Masterローカル) | text='{preview_text}'"
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
        style_id = self._extract_style_id(voice_name)
        encoded_text = urllib.parse.quote(text)
        eff_timeout = timeout_seconds or self.timeout_seconds
        req_timeout = aiohttp.ClientTimeout(total=eff_timeout)

        # 1. 音声合成用クエリの作成
        query_url = f"{target_url}/audio_query?text={encoded_text}&speaker={style_id}"
        try:
            async with session.post(query_url, timeout=req_timeout) as q_resp:
                if q_resp.status != 200:
                    err_msg = await q_resp.text()
                    logger.error(f"SHAREVOX audio_query failed on {target_url} ({q_resp.status}): {err_msg}")
                    raise TTSError(f"SHAREVOX audio_query HTTP {q_resp.status}: {err_msg}")
                query_data = await q_resp.json()
        except asyncio.TimeoutError:
            raise TTSConnectionError(f"SHAREVOX audio_query timed out on {target_url} after {eff_timeout}s")
        except aiohttp.ClientError as e:
            raise TTSConnectionError(f"SHAREVOX network error during audio_query on {target_url}: {e}")

        # 2. パラメータ調整
        pitch_scale = self._convert_pitch_to_scale(pitch)
        speaking_rate = max(0.5, min(2.0, float(speed)))
        query_data["speedScale"] = speaking_rate
        query_data["pitchScale"] = pitch_scale
        # 出力サンプリングレートを24000Hzに明示
        query_data["outputSamplingRate"] = 24000

        # 3. 音声波形合成
        synthesis_url = f"{target_url}/synthesis?speaker={style_id}"
        headers = {"Content-Type": "application/json"}
        try:
            async with session.post(synthesis_url, json=query_data, headers=headers, timeout=req_timeout) as s_resp:
                if s_resp.status == 200:
                    return await s_resp.read()
                else:
                    err_msg = await s_resp.text()
                    logger.error(f"SHAREVOX synthesis failed on {target_url} ({s_resp.status}): {err_msg}")
                    raise TTSError(f"SHAREVOX synthesis HTTP {s_resp.status}: {err_msg}")
        except asyncio.TimeoutError:
            raise TTSConnectionError(f"SHAREVOX synthesis timed out on {target_url} after {eff_timeout}s")
        except aiohttp.ClientError as e:
            raise TTSConnectionError(f"SHAREVOX network error during synthesis on {target_url}: {e}")
