#!/usr/bin/env python3
"""
全モデル試聴用サンプル音声（キャッシュ）一括事前生成スクリプト
- 対象テキスト: "こんにちは！超低遅延で自然な音声をお届けします。"
- パラメータ: pitch="+0%", speed=1.0, user_id=0
- 既存のキャッシュが存在する場合はスキップ
- 順序: クラウド・標準 (Edge/Azure/Google/OpenJTalk) -> VOICEVOX -> SHAREVOX -> COEIROINK
- 生成完了後、WebUI試聴はモデルロード不要で0秒即時再生が可能になる
"""

import os
import sys
import time
import asyncio
import logging

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import load_config
from src.database import Database
from src.cache_manager import CacheManager
from src.resource_manager import LocalEngineResourceManager
from src.tts.manager import TTSManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("pregenerate_preview")

SAMPLE_TEXT = "こんにちは！超低遅延で自然な音声をお届けします。"
SAMPLE_PITCH = "+0%"
SAMPLE_SPEED = 1.0
SAMPLE_USER_ID = 0


async def pregenerate_all():
    config = load_config()
    db = Database(config.db_path)
    cache_manager = CacheManager(
        cache_dir=config.cache_dir,
        max_size_bytes=config.cache_max_size_bytes,
        target_size_bytes=config.cache_target_size_bytes,
        voicevox_cache_dir=config.voicevox_cache_dir,
        voicevox_max_size_bytes=config.voicevox_cache_max_size_bytes,
        voicevox_target_size_bytes=config.voicevox_cache_target_size_bytes
    )
    resource_manager = LocalEngineResourceManager(bot=None, config=config, db=db)
    tts_manager = TTSManager(config, cache_manager, db, resource_manager=resource_manager)

    # 1. 永続キャッシュからモデル一覧を復元
    tts_manager.load_cached_voices()

    # 2. ローカルエンジン（VOICEVOX / SHAREVOX / COEIROINK）の最新モデルを動的取得
    for engine_name, init_fn in [
        ("VOICEVOX", tts_manager.initialize_voicevox),
        ("SHAREVOX", tts_manager.initialize_sharevox),
        ("COEIROINK", tts_manager.initialize_coeiroink),
    ]:
        try:
            prov = engine_name.lower()
            # コンテナが動いているか起動可能か確認
            if resource_manager.is_docker_available:
                await resource_manager.start_engine(prov, wait_healthy=True, timeout=8.0)
            await init_fn()
        except Exception as e:
            logger.warning(f"Engine {engine_name} initialization note: {e}")

    # 3. 対象話者の選定とプロバイダー別ソート (Edge/標準 -> VOICEVOX -> SHAREVOX -> COEIROINK)
    voices = list(config.voices.items())
    logger.info(f"Loaded {len(voices)} total voice models from registry.")

    def provider_priority(item):
        v = item[1]
        p = getattr(v, "provider", "")
        if p in ("edge", "azure", "google", "openjtalk"):
            return 1
        elif p == "voicevox":
            return 2
        elif p == "sharevox":
            return 3
        elif p == "coeiroink":
            return 4
        return 5

    voices.sort(key=provider_priority)

    total = len(voices)
    generated_count = 0
    skipped_count = 0
    failed_count = 0

    logger.info("=" * 60)
    logger.info(f"Starting pre-generation of preview audio for {total} models...")
    logger.info(f"Sample text: '{SAMPLE_TEXT}'")
    logger.info(f"Parameters: pitch={SAMPLE_PITCH}, speed={SAMPLE_SPEED}")
    logger.info("=" * 60)

    start_all_time = time.time()

    for idx, (spk_id, voice_cfg) in enumerate(voices, 1):
        prov = getattr(voice_cfg, "provider", "unknown")
        name = getattr(voice_cfg, "name", spk_id)

        # キャッシュキー確認
        cache_key = CacheManager.generate_cache_key(
            spk_id, SAMPLE_PITCH, SAMPLE_SPEED, SAMPLE_TEXT, user_id=SAMPLE_USER_ID
        )

        cached_file = await cache_manager.get(cache_key, provider=prov)
        if cached_file is not None and os.path.exists(cached_file):
            skipped_count += 1
            if idx % 20 == 0 or idx == total:
                logger.info(f"[{idx}/{total}] ({prov}) {name} -> Already cached (Skipped).")
            continue

        # 未生成の場合、必要ならエンジンを立ち上げて合成
        t0 = time.time()
        try:
            if prov in resource_manager.LOCAL_ENGINES:
                await resource_manager.start_engine(prov, wait_healthy=True, timeout=8.0)

            audio_path = await tts_manager.get_audio_path(
                user_id=SAMPLE_USER_ID,
                speaker_id=spk_id,
                pitch=SAMPLE_PITCH,
                speed=SAMPLE_SPEED,
                text=SAMPLE_TEXT
            )
            elapsed = time.time() - t0
            generated_count += 1
            logger.info(f"[{idx}/{total}] ✨ ({prov}) {name} generated in {elapsed:.2f}s -> {os.path.basename(audio_path)}")
        except Exception as e:
            failed_count += 1
            logger.error(f"[{idx}/{total}] ❌ Failed for ({prov}) {name}: {e}")

        # 短いスリープで過負荷防止
        await asyncio.sleep(0.05)

    total_elapsed = time.time() - start_all_time
    logger.info("=" * 60)
    logger.info("Pre-generation complete!")
    logger.info(f"Total: {total} | Generated: {generated_count} | Skipped: {skipped_count} | Failed: {failed_count}")
    logger.info(f"Total time: {total_elapsed:.1f}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(pregenerate_all())
