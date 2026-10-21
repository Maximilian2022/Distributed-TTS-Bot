#!/usr/bin/env python3
"""
未実在スタイル（実機エンジンに存在しないスタイル）を設定しているユーザーを、
実在する有効な話者へ自動再抽選・マイグレーションするスクリプト。
"""

import sys
import os
import sqlite3
import json
import random
import logging
import asyncio
from typing import Dict, List, Set

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("remigrate_voices")

async def main():
    # PYTHONPATH の設定
    sys.path.insert(0, "/app")
    from src.config import load_config
    from src.cache_manager import CacheManager
    from src.tts.manager import TTSManager

    db_path = os.getenv("BOT_DB_PATH", "/app/data/bot.db")
    if not os.path.exists(db_path):
        logger.error(f"Database not found at {db_path}")
        return

    logger.info("Initializing TTSManager to discover valid voice styles...")
    cfg = load_config("/app/config.yaml")
    cm = CacheManager("/app/cache")
    mgr = TTSManager(cfg, cm)

    # 各ローカルエンジンから実在話者を動的取得
    try:
        await mgr.initialize_voicevox()
    except Exception as e:
        logger.warning(f"Failed to init voicevox: {e}")

    try:
        await mgr.initialize_sharevox()
    except Exception as e:
        logger.warning(f"Failed to init sharevox: {e}")

    # COEIROINK 実機エンジンから直接取得した実在スタイルのみを厳密抽出
    real_ci_ids = set()
    try:
        spks = await mgr.coeiroink_client.get_speakers()
        if spks:
            for spk in spks:
                u = str(spk.get("speakerUuid", "")).strip().lower()
                for st in spk.get("styles", []):
                    sid = st.get("styleId")
                    real_ci_ids.add(f"ci_{sid}")
                    if u:
                        real_ci_ids.add(f"ci_{u[:8]}_{sid}")
            logger.info(f"Discovered {len(spks)} COEIROINK characters, total {sum(len(s.get('styles', [])) for s in spks)} real styles from engine API.")
    except Exception as e:
        logger.error(f"Failed to fetch COEIROINK engine speakers: {e}")

    # 実在する有効話者一覧（ID → VoiceConfig）
    # COEIROINK であって real_ci_ids に含まれないものは未実在スタイルとして除外
    valid_voices: Dict[str, any] = {}
    purged_cache_ids = []
    for vid, v in cfg.voices.items():
        if v.provider == "coeiroink":
            if vid in real_ci_ids:
                valid_voices[vid] = v
            else:
                purged_cache_ids.append(vid)
        else:
            valid_voices[vid] = v

    logger.info(f"Total valid voice models available in system: {len(valid_voices)} (purged {len(purged_cache_ids)} non-existent styles: {purged_cache_ids[:5]}...)")

    # キャッシュファイル voices_cache.json もクリーンアップ
    cache_file = "/app/data/voices_cache.json"
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            orig_voices = cdata.get("voices", [])
            clean_voices = [
                v for v in orig_voices
                if v.get("provider") != "coeiroink" or v.get("id") in real_ci_ids
            ]
            cdata["voices"] = clean_voices
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cdata, f, ensure_ascii=False, indent=2)
            logger.info(f"Cleaned up {cache_file}: reduced from {len(orig_voices)} to {len(clean_voices)} cached voices.")
        except Exception as e:
            logger.error(f"Failed to clean cache file: {e}")

    # 安全のため、最低限の有効ボイスが存在することを確認
    if len(valid_voices) < 50:
        logger.error(f"Too few valid voices ({len(valid_voices)}). Aborting migration for safety.")
        await mgr.close()
        return

    # 再抽選用候補リスト（日本語で聞き取りやすい代表的なローカル・クラウドボイス群）
    # VOICEVOX, SHAREVOX, COEIROINK(実在93種), EdgeTTS が含まれる
    candidate_voice_ids = [
        vid for vid, v in valid_voices.items()
        if v.provider in ("voicevox", "sharevox", "coeiroink", "edge")
    ]
    if not candidate_voice_ids:
        candidate_voice_ids = list(valid_voices.keys())

    logger.info(f"Candidate voice pool for re-roll: {len(candidate_voice_ids)} voices")

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    updated_user_settings = 0
    updated_guild_user_settings = 0
    updated_guild_settings = 0

    # 1. user_settings の再抽選
    c.execute("SELECT user_id, speaker_id FROM user_settings")
    rows = c.fetchall()
    for user_id, speaker_id in rows:
        if speaker_id not in valid_voices:
            # 未実在スタイルのため再抽選
            new_speaker = random.choice(candidate_voice_ids)
            new_voice_info = valid_voices[new_speaker]
            c.execute(
                "UPDATE user_settings SET speaker_id = ? WHERE user_id = ?",
                (new_speaker, user_id)
            )
            updated_user_settings += 1
            logger.info(
                f"[user_settings] User {user_id}: re-rolled invalid '{speaker_id}' -> '{new_speaker}' ({new_voice_info.name} / {new_voice_info.provider})"
            )

    # 2. guild_user_settings の再抽選
    c.execute("SELECT guild_id, user_id, speaker_id FROM guild_user_settings")
    rows = c.fetchall()
    for guild_id, user_id, speaker_id in rows:
        if speaker_id not in valid_voices:
            # 未実在スタイルのため再抽選
            new_speaker = random.choice(candidate_voice_ids)
            new_voice_info = valid_voices[new_speaker]
            c.execute(
                "UPDATE guild_user_settings SET speaker_id = ? WHERE guild_id = ? AND user_id = ?",
                (new_speaker, guild_id, user_id)
            )
            updated_guild_user_settings += 1
            logger.info(
                f"[guild_user_settings] Guild {guild_id}, User {user_id}: re-rolled invalid '{speaker_id}' -> '{new_speaker}' ({new_voice_info.name} / {new_voice_info.provider})"
            )

    # 3. guild_settings の再抽選
    c.execute("SELECT guild_id, default_speaker FROM guild_settings")
    rows = c.fetchall()
    for guild_id, default_speaker in rows:
        if default_speaker and default_speaker not in valid_voices:
            new_speaker = random.choice(candidate_voice_ids)
            new_voice_info = valid_voices[new_speaker]
            c.execute(
                "UPDATE guild_settings SET default_speaker = ? WHERE guild_id = ?",
                (new_speaker, guild_id)
            )
            updated_guild_settings += 1
            logger.info(
                f"[guild_settings] Guild {guild_id}: re-rolled invalid default_speaker '{default_speaker}' -> '{new_speaker}' ({new_voice_info.name})"
            )

    conn.commit()
    conn.close()
    await mgr.close()

    logger.info("=== Migration Summary ===")
    logger.info(f"user_settings updated: {updated_user_settings} records")
    logger.info(f"guild_user_settings updated: {updated_guild_user_settings} records")
    logger.info(f"guild_settings updated: {updated_guild_settings} records")
    logger.info("Database migration completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
