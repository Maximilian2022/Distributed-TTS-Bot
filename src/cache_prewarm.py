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
    アイドル時間帯（最後のVC発言から一定時間経過後）および早朝最閑散時間帯（JST 04:30）に、
    頻出フレーズや再利用実績の高いホットワードのキャッシュをバックグラウンドで事前合成（投機的生成）し、
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
        self._speculative_running = False
        self.last_speculative_run_time: Optional[float] = None
        self.last_speculative_stats: Dict[str, Any] = {}
        self._last_speculative_date: str = ""

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
        logger.info("🚀 Intelligent Prewarm Service started (Background Cache Warm-up & Daily Speculative Prewarm)")

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

                # 1. 早朝 (JST 04:30 〜 04:50) の日次投機的生成 (1日1回・最閑散時間帯に実行)
                from datetime import datetime, timezone, timedelta
                jst_now = datetime.now(timezone(timedelta(hours=9)))
                today_str = jst_now.strftime("%Y-%m-%d")
                if jst_now.hour == 4 and 30 <= jst_now.minute <= 50:
                    if self._last_speculative_date != today_str:
                        self._last_speculative_date = today_str
                        logger.info(f"🌅 [Intelligent Prewarm] Early morning daily speculative prewarm triggered (JST {jst_now.strftime('%H:%M:%S')})...")
                        asyncio.create_task(
                            self.run_speculative_prewarm(days=3, min_hits=3, max_candidates=30),
                            name="daily_speculative_prewarm"
                        )

                # 2. 日中アイドル時の固定フレーズ自動ウォーム
                # ユーザーが現在発言中、または直近の会話から一定時間経過していない場合はスキップ
                now = time.time()
                if self._busy or (now - self._last_user_speech_time) < self.idle_threshold_seconds:
                    continue

                # 投機的生成が実行中であれば通常アイドル処理は譲る
                if self._speculative_running:
                    continue

                # 優先度の高い話者リストを取得
                target_speakers = self._get_target_speakers()
                if not target_speakers:
                    continue

                # 頻出固定フレーズを1つずつチェックし、未キャッシュのものをゆっくり事前合成
                for phrase in COMMON_PHRASES:
                    if not self._running or self._speculative_running:
                        break
                    if self._busy or (time.time() - self._last_user_speech_time) < self.idle_threshold_seconds:
                        break

                    for spk in target_speakers:
                        if not self._running or self._speculative_running or self._busy or (time.time() - self._last_user_speech_time) < self.idle_threshold_seconds:
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

    async def run_speculative_prewarm(
        self,
        days: int = 3,
        min_hits: int = 3,
        max_candidates: int = 30
    ) -> Dict[str, Any]:
        """
        直近 days 日間で min_hits 回以上使われた頻出単語・相槌・名前プレフィックス等を
        DBから動的に抽出し、未キャッシュのものを先行合成（投機的生成）する。
        """
        if self._speculative_running:
            logger.info("ℹ️ [Speculative Prewarm] Already running, skipping request.")
            return {"status": "already_running", "generated": 0, "skipped": 0}

        self._speculative_running = True
        start_time = time.time()
        generated_count = 0
        skipped_count = 0
        target_phrases: List[str] = []

        try:
            logger.info(f"🔥 [Speculative Prewarm] Starting speculative hotword warm-up (days={days}, min_hits={min_hits}, max_candidates={max_candidates})...")

            # DBから頻出フレーズ候補を抽出
            if hasattr(self.db, "get_frequent_phrases_for_prewarm"):
                candidates = await self.db.get_frequent_phrases_for_prewarm(
                    days=days,
                    min_hits=min_hits,
                    max_length=15,
                    limit=max_candidates
                )
                target_phrases = [c["text"] for c in candidates if c.get("text")]

            if not target_phrases:
                logger.info("ℹ️ [Speculative Prewarm] No candidate phrases found matching criteria.")
                stats = {"status": "no_candidates", "generated": 0, "skipped": 0, "duration_seconds": 0}
                self.last_speculative_stats = stats
                return stats

            # 対象話者リストの取得（アクティブユーザーの設定話者優先）
            target_speakers = await self._get_speculative_target_speakers()
            if not target_speakers:
                target_speakers = self._get_target_speakers()

            logger.info(f"🎯 [Speculative Prewarm] Candidates: {len(target_phrases)} phrases, Speakers: {target_speakers}")

            from src.cache_manager import CacheManager
            for phrase in target_phrases:
                if not self._running:
                    break
                # 会話が発生している間は安全に一時待機
                while self._busy:
                    await asyncio.sleep(2.0)

                for spk in target_speakers:
                    if not self._running:
                        break
                    while self._busy:
                        await asyncio.sleep(2.0)

                    voice_cfg = getattr(self.tts, "_get_voice_config", lambda s: None)(spk)
                    provider = voice_cfg.provider if voice_cfg else "edge"
                    cache_key = CacheManager.generate_cache_key(spk, "0", 1.0, phrase, user_id=0)

                    cached = await self.cache.get(cache_key, provider=provider)
                    if cached is not None:
                        skipped_count += 1
                        continue

                    try:
                        logger.info(f"🌱 [Speculative Prewarm] Generating: '{phrase}' (speaker={spk}, provider={provider})")
                        await self.tts.get_audio_path(
                            user_id=0,
                            speaker_id=spk,
                            pitch="0",
                            speed=1.0,
                            text=phrase
                        )
                        generated_count += 1
                        # 最閑散時間帯はインターバル2秒、会話アイドル時は短め
                        await asyncio.sleep(2.0)
                    except Exception as e:
                        logger.warning(f"Failed to generate speculative cache for '{phrase}' ({spk}): {e}")
                        await asyncio.sleep(2.0)

            duration = round(time.time() - start_time, 2)
            stats = {
                "status": "completed",
                "generated": generated_count,
                "skipped": skipped_count,
                "phrases_count": len(target_phrases),
                "speakers_count": len(target_speakers),
                "duration_seconds": duration,
                "timestamp": time.time()
            }
            self.last_speculative_stats = stats
            self.last_speculative_run_time = time.time()
            logger.info(f"✅ [Speculative Prewarm] Finished in {duration}s: {generated_count} generated, {skipped_count} skipped.")
            return stats

        except Exception as e:
            logger.error(f"❌ [Speculative Prewarm] Error: {e}", exc_info=True)
            stats = {"status": "error", "error": str(e), "generated": generated_count, "skipped": skipped_count}
            self.last_speculative_stats = stats
            return stats
        finally:
            self._speculative_running = False

    async def _get_speculative_target_speakers(self) -> List[str]:
        """投機的キャッシュ生成用の対象話者（アクティブユーザーの話者優先）を取得"""
        speakers: List[str] = []
        voices = getattr(self.config, "voices", {})

        def_spk = getattr(self.config, "default_speaker", "nanami")
        if def_spk and def_spk in voices:
            speakers.append(def_spk)

        # 実際のユーザーが設定している話者をDBから抽出
        try:
            if hasattr(self.db, "get_connection"):
                async with self.db.get_connection() as db:
                    cur = await db.execute("SELECT DISTINCT speaker FROM user_settings WHERE speaker IS NOT NULL LIMIT 8")
                    rows = await cur.fetchall()
                    for r in rows:
                        spk = str(r[0])
                        if spk in voices and spk not in speakers:
                            speakers.append(spk)
        except Exception as e:
            logger.debug(f"Failed to query active speakers from user_settings: {e}")

        # 代表的な人気話者を追加
        popular = ["nanami", "keiko", "1", "2", "3"]
        for p in popular:
            if p in voices and p not in speakers:
                speakers.append(p)
            if len(speakers) >= 5:
                break

        return speakers[:5]

    def _get_target_speakers(self) -> List[str]:
        """事前キャッシュの対象とする代表的な話者IDリストを返却"""
        speakers: List[str] = []
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
