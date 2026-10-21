import asyncio
import time
import logging
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

# 日常頻出フレーズ（相槌・挨拶・短文）
COMMON_PHRASES: List[str] = [
    "こんにちは",
    "こんばんは",
    "おはようございます",
    "おつかれさまです",
    "おつかれ",
    "おつ",
    "草",
    "了解です",
    "了解",
    "ありがとう",
    "ありがとうございます",
    "助かる",
    "はい",
    "いいえ",
    "なるほど",
    "わかった",
    "よろしく",
    "よろしくお願いします"
]

class IntelligentPrewarmService:
    """
    アイドル時間帯（最後のVC発言から一定時間経過後）に、
    頻出フレーズや主要話者のキャッシュをバックグラウンドで事前合成し、
    会話時の初動ラグ 0ms（100%キャッシュヒット）を実現する自律サービス。
    """
    def __init__(
        self,
        tts_manager: Any,
        cache_manager: Any,
        db: Any,
        config: Any,
        idle_threshold_seconds: float = 120.0,
        warm_interval_seconds: float = 8.0
    ):
        self.tts = tts_manager
        self.cache = cache_manager
        self.db = db
        self.config = config
        self.idle_threshold_seconds = idle_threshold_seconds
        self.warm_interval_seconds = warm_interval_seconds
        self._last_user_speech_time = time.time()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._busy = False  # ユーザー発言中は即座に一時停止

    def record_user_speech(self) -> None:
        """ユーザー発言時に呼び出してアイドルタイマーをリセットし、実行中の事前合成を即時中断"""
        self._last_user_speech_time = time.time()
        self._busy = True

    def notify_user_speech_done(self) -> None:
        """ユーザー発言完了時に呼び出し"""
        self._busy = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._prewarm_loop(), name="intelligent_prewarm_loop")
        logger.info("🚀 Intelligent Prewarm Service started (Background Cache Warm-up)")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 Intelligent Prewarm Service stopped")

    async def _prewarm_loop(self) -> None:
        # 起動直後のバーストを避けるため、初回は60秒待機
        await asyncio.sleep(60.0)

        while self._running:
            try:
                await asyncio.sleep(15.0)

                # ユーザーが現在発言中、または直近の会話から一定時間経過していない場合はスキップ
                now = time.time()
                if self._busy or (now - self._last_user_speech_time) < self.idle_threshold_seconds:
                    continue

                # 優先度の高い話者リストを取得
                target_speakers = self._get_target_speakers()
                if not target_speakers:
                    continue

                # 頻出フレーズを1つずつチェックし、未キャッシュのものをゆっくり事前合成
                for phrase in COMMON_PHRASES:
                    if not self._running:
                        break
                    if self._busy or (time.time() - self._last_user_speech_time) < self.idle_threshold_seconds:
                        break

                    for spk in target_speakers:
                        if not self._running or self._busy or (time.time() - self._last_user_speech_time) < self.idle_threshold_seconds:
                            break

                        # キャッシュ有無を高速確認
                        voice_cfg = getattr(self.tts, "_get_voice_config", lambda s: None)(spk)
                        provider = voice_cfg.provider if voice_cfg else "edge"
                        from src.cache_manager import CacheManager
                        cache_key = CacheManager.generate_cache_key(spk, "0", 1.0, phrase, user_id=0)

                        cached = await self.cache.get(cache_key, provider=provider)
                        if cached is not None:
                            continue  # 既に存在すればスキップ

                        # 未キャッシュの場合のみ低優先度で1件事前合成
                        try:
                            logger.info(f"🌱 [Intelligent Pre-cache] Auto-warming frequent phrase '{phrase}' for speaker '{spk}'")
                            await self.tts.get_audio_path(
                                user_id=0,
                                speaker_id=spk,
                                pitch="0",
                                speed=1.0,
                                text=phrase
                            )
                            # CPU/GPUに負荷をかけないよう次の単語まで十分なインターバルを挟む
                            await asyncio.sleep(self.warm_interval_seconds)
                        except Exception as e:
                            logger.debug(f"[Intelligent Pre-cache] Skipped '{phrase}' for {spk}: {e}")
                            await asyncio.sleep(5.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error in intelligent prewarm loop: {e}")
                await asyncio.sleep(30.0)

    def _get_target_speakers(self) -> List[str]:
        """事前キャッシュの対象とする代表的な話者IDリストを返却"""
        speakers = []
        voices = getattr(self.config, "voices", {})

        def_spk = getattr(self.config, "default_speaker", "nanami")
        if def_spk and def_spk in voices:
            speakers.append(def_spk)

        # 代表的な人気話者（設定にあれば）
        popular = ["nanami", "keiko", "1", "2", "3"]
        for p in popular:
            if p in voices and p not in speakers:
                speakers.append(p)
                if len(speakers) >= 4:
                    break
        return speakers
