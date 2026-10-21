#!/usr/bin/env python3
"""
ローカルTTSエンジン（VOICEVOX, SHAREVOX, COEIROINK）のコンテナログを監視し、
logs/engine.log に集約・ローテーション管理するデーモンスクリプト。
"""
import sys
import os
import time
import signal
import subprocess
import threading
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional

# プロジェクトルートを追加
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.utils.log_handler import TwoStageZstdRotatingHandler

# ログファイルパス
LOG_FILE = BASE_DIR / "logs" / "engine.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ロガー設定 (最大3000行、直近100行保持、100MB超過時zstd圧縮、10世代、1GB上限清掃)
logger = logging.getLogger("EngineAggregator")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

file_handler = TwoStageZstdRotatingHandler(
    str(LOG_FILE),
    max_lines=3000,
    keep_lines=100,
    archive_max_bytes=100 * 1024 * 1024,
    max_archives=10,
    dir_max_bytes=1024 * 1024 * 1024,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# コンソールにも出力
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

TARGET_CONTAINERS = [
    ("VOICEVOX", ["ttsworker-voicevox", "ttsbot-voicevox"]),
    ("SHAREVOX", ["ttsworker-sharevox", "ttsbot-sharevox"]),
    ("COEIROINK", ["ttsworker-coeiroink", "ttsbot-coeiroink"]),
]

stop_event = threading.Event()

def is_container_running(container_name: str) -> bool:
    """Dockerコンテナが稼働中かどうかを確認"""
    try:
        res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except Exception:
        return False

def find_running_container(candidates: List[str]) -> Optional[str]:
    """候補の中から現在稼働中のコンテナ名を探す"""
    for c in candidates:
        if is_container_running(c):
            return c
    return None

def monitor_container_logs(engine_name: str, container_candidates: List[str]):
    """コンテナのログをリアルタイムで追跡し、engine.log に書き込む"""
    tail_lines = "20"
    last_state_running = None

    while not stop_event.is_set():
        try:
            # 稼働中のコンテナを特定
            container_name = find_running_container(container_candidates)
            if not container_name:
                if last_state_running is not False:
                    last_state_running = False
                time.sleep(3)
                continue

            if last_state_running is False:
                # 停止状態から再起動した場合は新規ログから追尾
                tail_lines = "0"
            last_state_running = True

            cmd = ["docker", "logs", "-f", "--tail", tail_lines, container_name]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )

            # 次回以降は新規ログのみ追跡
            tail_lines = "0"

            while not stop_event.is_set():
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                line_str = line.strip()
                if line_str:
                    logger.info(f"[{engine_name}] {line_str}")

            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

            # プロセス終了後の待機
            time.sleep(1)

        except Exception as e:
            if not stop_event.is_set():
                logger.warning(f"[{engine_name}] ログ取得エラー ({container_candidates}): {e}. 5秒後に再試行します...")
                time.sleep(5)

def main():
    def signal_handler(signum, frame):
        logger.info("[SYSTEM] ログ集約デーモンを停止します...")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("[SYSTEM] 各種ローカルエンジン (VOICEVOX, SHAREVOX, COEIROINK) のログ集約を開始しました。")

    threads = []
    for engine_name, container_candidates in TARGET_CONTAINERS:
        t = threading.Thread(
            target=monitor_container_logs,
            args=(engine_name, container_candidates),
            daemon=True,
            name=f"log-{engine_name}"
        )
        t.start()
        threads.append(t)

    while not stop_event.is_set():
        time.sleep(1)

    logger.info("[SYSTEM] ログ集約デーモンが正常に停止しました。")

if __name__ == "__main__":
    main()
