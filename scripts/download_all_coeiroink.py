#!/usr/bin/env python3
import os
import sys
import time
import subprocess
from pathlib import Path

# パス追加
sys.path.insert(0, '/root/ttsbot')
from src.tts.coeiroink_models import CoeiroinkModelManager

MODEL_DIR = "/var/lib/docker/volumes/coeiroink_models/_data"

def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting full COEIROINK official models download to {MODEL_DIR}...")
    mgr = CoeiroinkModelManager(model_dir=MODEL_DIR)
    status = mgr.get_status()
    summary = status["summary"]
    print(f"Initial status: {summary['installed_speakers']}/{summary['total_speakers']} speakers, {summary['installed_styles']}/{summary['total_styles']} styles installed.")

    official = mgr.get_official_speakers()
    for idx, sp in enumerate(official, 1):
        prefix = sp["prefix"]
        name = sp["speakerName"]
        total_sp_styles = len(sp.get("styles", []))

        # 現在のステータス確認
        current_status = mgr.get_status()
        sp_stat = next((s for s in current_status["speakers"] if s["prefix"] == prefix), None)
        if sp_stat and sp_stat["is_complete"]:
            print(f"[{idx}/{len(official)}] Speaker '{name}' ({prefix}) is already fully installed ({total_sp_styles}/{total_sp_styles} styles). Skipping.")
            continue

        print(f"[{idx}/{len(official)}] Downloading '{name}' ({prefix}) - {total_sp_styles} styles...")
        t0 = time.time()
        ok = mgr.download_speaker_sync(prefix)
        elapsed = time.time() - t0
        print(f"[{idx}/{len(official)}] Finished '{name}' in {elapsed:.1f}s (Success: {ok})")

    final_status = mgr.get_status()
    f_summary = final_status["summary"]
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] All downloads finished! Total: {f_summary['installed_speakers']}/{f_summary['total_speakers']} speakers, {f_summary['installed_styles']}/{f_summary['total_styles']} styles.")

    # COEIROINK エンジンを再起動して新モデルをロード
    print("Restarting ttsbot-coeiroink container...")
    subprocess.run(["docker", "restart", "ttsbot-coeiroink"], check=True)
    time.sleep(3)

    # ttsbot コンテナ内で話者リストを最新化
    print("Reloading speakers in discord-tts-bot...")
    subprocess.run([
        "docker", "exec", "discord-tts-bot", "python3", "-c",
        "import asyncio, urllib.request, json;"
        "from src.tts.manager import TTSManager;"
        "from src.config import load_config;"
        "from src.database import Database;"
        "from src.cache_manager import CacheManager;"
        "cfg = load_config(); db = Database(cfg.db_path); cm = CacheManager('/app/cache', voicevox_cache_dir='/app/cache_voicevox');"
        "mgr = TTSManager(cfg, cm, db);"
        "count = asyncio.run(mgr.initialize_coeiroink());"
        "print(f'Successfully reloaded {count} COEIROINK models into registry!')"
    ], check=True)
    print("All tasks completed successfully!")

if __name__ == "__main__":
    main()
