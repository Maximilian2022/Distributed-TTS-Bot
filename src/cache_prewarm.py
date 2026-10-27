import os
import json
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

            # DBから頻出フレーズ候補を抽出 (歴代定番TOP30 + 直近トレンドTOP30 のハイブリッド)
            if hasattr(self.db, "get_frequent_phrases_for_prewarm"):
                candidates_res = await self.db.get_frequent_phrases_for_prewarm(
                    days=days,
                    min_hits=min_hits,
                    max_length=15,
                    limit=max_candidates
                )
                if isinstance(candidates_res, dict):
                    target_phrases = [c["text"] for c in candidates_res.get("combined", []) if c.get("text")]
                    self.last_speculative_candidates = candidates_res
                else:
                    target_phrases = [c["text"] for c in candidates_res if c.get("text")]

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

            # 1. ローカルプロバイダー（voicevox, coeiroink, sharevox）の事前一括起動
            # 投機実行は最閑散時間帯（VC無人時）に実行されるため、ローカルエンジンがスリープ停止している。
            # 合成前にあらかじめ確実に起動させ、Edge-TTSへの意図しないフォールバックを防止する。
            rm = getattr(self.tts, "resource_manager", None)
            local_providers_needed = {
                self._resolve_speaker_provider(s) for s in target_speakers 
                if self._resolve_speaker_provider(s) in ("voicevox", "coeiroink", "sharevox")
            }
            if rm and local_providers_needed:
                for prov in local_providers_needed:
                    logger.info(f"⚡ [Speculative Prewarm] Ensuring local engine '{prov}' is running before batch...")
                    try:
                        await rm.start_engine(prov, wait_healthy=True, timeout=15.0)
                    except Exception as e:
                        logger.warning(f"⚠️ [Speculative Prewarm] Failed to pre-start local engine '{prov}': {e}")

            from src.cache_manager import CacheManager

            # 2. プロバイダーごとにグループ化して合成（エンジン切替の負荷と競合を低減）
            # クラウド（Azure, Google）を先に処理し、次にローカル（VOICEVOX, COEIROINK, SHAREVOX）
            speakers_by_provider: Dict[str, List[str]] = {}
            for spk in target_speakers:
                prov = self._resolve_speaker_provider(spk)
                speakers_by_provider.setdefault(prov, []).append(spk)

            provider_order = ["azure", "google", "voicevox", "coeiroink", "sharevox"]
            sorted_providers = sorted(
                speakers_by_provider.keys(),
                key=lambda p: provider_order.index(p) if p in provider_order else 99
            )

            for prov in sorted_providers:
                if not self._running:
                    break
                prov_speakers = speakers_by_provider[prov]

                # ローカルプロバイダー処理直前の再確認（万が一停止していた場合の起動）
                if rm and prov in ("voicevox", "coeiroink", "sharevox"):
                    try:
                        await rm.start_engine(prov, wait_healthy=True, timeout=10.0)
                    except Exception:
                        pass

                for phrase in target_phrases:
                    if not self._running:
                        break
                    while self._busy:
                        await asyncio.sleep(2.0)

                    for spk in prov_speakers:
                        if not self._running:
                            break
                        while self._busy:
                            await asyncio.sleep(2.0)

                        cache_key = CacheManager.generate_cache_key(spk, "0", 1.0, phrase, user_id=0)
                        cached = await self.cache.get(cache_key, provider=prov)
                        if cached is not None:
                            skipped_count += 1
                            continue

                        try:
                            spk_name = self.get_speaker_display_name(spk)
                            logger.info(f"🌱 [Speculative Prewarm] Generating: '{phrase}' (speaker={spk_name} [{spk}], provider={prov})")
                            await self.tts.get_audio_path(
                                user_id=0,
                                speaker_id=spk,
                                pitch="0",
                                speed=1.0,
                                text=phrase,
                                speculative_timeout=999.0
                            )

                            # フォールバック発生チェック (本来のローカル話者でなくEdge-TTS等に化けた場合はスキップ扱い)
                            meta = getattr(self.tts, "last_synthesis_meta", {})
                            if meta.get("is_fallback") or (meta.get("provider") and meta.get("provider") != prov):
                                logger.warning(
                                    f"⚠️ [Speculative Prewarm] Fallback detected for '{phrase}' "
                                    f"(requested={prov}/{spk}, actual={meta.get('provider')}). Skipping count."
                                )
                                skipped_count += 1
                            else:
                                generated_count += 1

                            # 最閑散時間帯はインターバル1.5秒、会話アイドル時は短め
                            await asyncio.sleep(1.5)
                        except Exception as e:
                            logger.warning(f"Failed to generate speculative cache for '{phrase}' ({spk}): {e}")
                            await asyncio.sleep(2.0)

            # 3. 投機事前生成完了後、VCが無人であればローカルエンジンを安全に停止しメモリ返却
            if rm and not rm.is_any_human_in_vc():
                logger.info("💤 [Speculative Prewarm] VC is vacant. Stopping local engines to restore idle power/memory state...")
                try:
                    await rm.stop_all_active_engines(reason="Speculative Prewarm Completed")
                except Exception as e:
                    logger.debug(f"Error stopping engines after speculative prewarm: {e}")

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
            logger.info(f"✅ [Speculative Prewarm] Finished in {duration}s: {generated_count} generated, {skipped_count} skipped across {len(target_speakers)} speakers.")
            return stats

        except Exception as e:
            logger.error(f"❌ [Speculative Prewarm] Error: {e}", exc_info=True)
            stats = {"status": "error", "error": str(e), "generated": generated_count, "skipped": skipped_count}
            self.last_speculative_stats = stats
            return stats
        finally:
            self._speculative_running = False

    def _resolve_speaker_provider(self, speaker_id: str) -> str:
        """話者IDから対応プロバイダーを正確に特定（ローカル/クラウドのキャッシュディレクトリ振り分け用）"""
        spk = str(speaker_id).strip()
        voice_cfg = getattr(self.tts, "_get_voice_config", lambda s: None)(spk)
        if voice_cfg and getattr(voice_cfg, "id", "") == spk:
            return getattr(voice_cfg, "provider", "edge")
        if spk.startswith("vv_"):
            return "voicevox"
        if spk.startswith("ci_"):
            return "coeiroink"
        if spk.startswith("sv_"):
            return "sharevox"
        if spk.startswith("g_"):
            return "google"
        return "azure"

    async def _get_speculative_target_speakers(self) -> List[str]:
        """
        投機的キャッシュ生成用の対象話者を取得。
        クラウド・ローカル問わず、現在ユーザー（user_settings）またはサーバー別（guild_user_settings）で
        実際に利用設定されている全モデルを抽出する。
        今後新しい音声モデルが設定された場合も、翌朝の投機キャッシュ生成に自動連動する。
        """
        speakers: List[str] = []

        # 1. デフォルト話者
        def_spk = getattr(self.config, "default_speaker", "nanami")
        if def_spk:
            speakers.append(def_spk)

        # 2. 実際にユーザーやギルドで設定されている全音声モデルをDBから動的取得
        if hasattr(self.db, "get_configured_speakers"):
            try:
                db_speakers = await self.db.get_configured_speakers()
                for spk in db_speakers:
                    if spk not in speakers:
                        speakers.append(spk)
            except Exception as e:
                logger.warning(f"Error fetching configured speakers from DB: {e}")

        # 3. 代表フォールバック話者（未登録時のセーフティネット）
        for p in ["nanami", "keita", "g_wavenet_c", "vv_10", "ci_90"]:
            if p not in speakers:
                speakers.append(p)

        logger.info(f"🎙️ [Speculative Prewarm] Target configured speakers ({len(speakers)} models): {speakers}")
        return speakers

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

    def get_speaker_display_name(self, speaker_id: str) -> str:
        """話者IDから人間が直感的に判別できる表示名（例: 雨晴はう (ノーマル)）を解決"""
        spk = str(speaker_id).strip()

        # 1. config.voices
        if self.config and hasattr(self.config, "voices"):
            v = self.config.voices.get(spk)
            if v and getattr(v, "name", None):
                return v.name

        # 2. tts._get_voice_config
        if self.tts and hasattr(self.tts, "_get_voice_config"):
            try:
                v = self.tts._get_voice_config(spk)
                if v and getattr(v, "name", None) and getattr(v, "id", "") == spk:
                    return v.name
            except Exception:
                pass

        # 3. data/voices_cache.json
        if not hasattr(self, "_voices_cache_names"):
            self._voices_cache_names = {}
            for path in ["data/voices_cache.json", "/app/data/voices_cache.json"]:
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            d = json.load(f)
                            for item in d.get("voices", []):
                                if "id" in item and "name" in item:
                                    self._voices_cache_names[item["id"]] = item["name"]
                        break
                    except Exception:
                        pass
        if spk in self._voices_cache_names:
            return self._voices_cache_names[spk]

        # 4. Google音声のデフォルト整形
        if spk.startswith("g_"):
            parts = spk.split("_")
            if len(parts) >= 3:
                return f"Google {parts[1].capitalize()} {parts[2].upper()}"

        return spk
