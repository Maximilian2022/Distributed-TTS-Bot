#!/usr/bin/env python3
"""
Hayamimi Game Bridge Client (Windows / ゲームチャット自動入力)
Discord VC で自分が喋った確定字幕テキストを受信し、
アクティブなゲームチャット入力欄に「クリップボード貼り付け ➔ Enter」を自動実行します。
"""

import asyncio
import configparser
import json
import logging
import os
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    print("[!] 'websockets' がインストールされていません。uv pip install websockets を実行してください。")
    sys.exit(1)

try:
    import pyperclip
except ImportError:
    print("[!] 'pyperclip' がインストールされていません。uv pip install pyperclip を実行してください。")
    sys.exit(1)

try:
    import pyautogui
except ImportError:
    print("[!] 'pyautogui' がインストールされていません。uv pip install pyautogui を実行してください。")
    sys.exit(1)

# pyautogui のセーフティ（画面左上端にマウスを動かすと緊急停止）
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.02

CONFIG_FILE = Path(__file__).resolve().parent / "config.ini"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("GameBridge")


def load_or_create_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        config.read(CONFIG_FILE, encoding="utf-8")
        return config

    # 初回対話生成
    print("=" * 60)
    print("🎙️ Hayamimi Game Bridge - 初回初期設定")
    print("=" * 60)
    server_url = input("TTSBot の WebUI / API URL (例: ws://192.168.11.244:8088): ").strip()
    if not server_url:
        server_url = "ws://192.168.11.244:8088"
    if not server_url.startswith(("ws://", "wss://")):
        server_url = server_url.replace("http://", "ws://").replace("https://", "wss://")
        if not server_url.startswith(("ws://", "wss://")):
            server_url = f"ws://{server_url}"

    user_id = input("あなたの Discord ユーザー ID (空欄で全話者対象): ").strip()

    config["settings"] = {
        "server_url": server_url,
        "target_user_id": user_id or "0",
        "enter_delay_ms": "50",
        "auto_enter": "true",
        "paused": "false",
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        config.write(f)
    print(f"✅ 設定ファイルを生成しました: {CONFIG_FILE}")
    print("=" * 60)
    return config


class GameBridgeClient:
    def __init__(self, config: configparser.ConfigParser):
        self.config = config
        self.server_base = config.get("settings", "server_url", fallback="ws://192.168.11.244:8088").rstrip("/")
        self.target_user_id = config.get("settings", "target_user_id", fallback="0").strip()
        self.enter_delay = float(config.get("settings", "enter_delay_ms", fallback="50")) / 1000.0
        self.auto_enter = config.getboolean("settings", "auto_enter", fallback=True)
        self.paused = config.getboolean("settings", "paused", fallback=False)

    def build_ws_url(self) -> str:
        base = self.server_base
        if "/api/stt/stream" not in base:
            base = f"{base}/api/stt/stream"
        return f"{base}?user_id={self.target_user_id}"

    def paste_and_send(self, text: str, user_name: str) -> None:
        """クリップボードにコピーしてゲームチャットに貼り付け＆Enter"""
        if self.paused:
            logger.info(f"⏸️ [一時停止中] スキップ: {text}")
            return

        try:
            # 1. クリップボードへコピー
            pyperclip.copy(text)
            time.sleep(0.01)

            # 2. Ctrl + V で貼り付け (IME誤変換や取りこぼしを完全回避)
            pyautogui.hotkey("ctrl", "v")

            # 3. Enter キーで送信
            if self.auto_enter:
                if self.enter_delay > 0:
                    time.sleep(self.enter_delay)
                pyautogui.press("enter")

            logger.info(f"🚀 [入力完了] {user_name}: \"{text}\"")
        except Exception as e:
            logger.error(f"❌ 入力自動化エラー: {e}")

    async def run(self):
        ws_url = self.build_ws_url()
        print("\n" + "=" * 60)
        print("🎮 Hayamimi Game Bridge - 稼働中")
        print(f"🌐 接続先: {ws_url}")
        print(f"🎯 対象ユーザーID: {self.target_user_id or '全員 (All)'}")
        print("💡 ゲーム内のテキストチャット入力欄にカーソルを合わせておいてください。")
        print("⚠️ 緊急停止: マウスカーソルを画面の一番左上隅 (0,0) に勢いよく動かすと強制停止します。")
        print("=" * 60 + "\n")

        while True:
            try:
                logger.info(f"Connecting to {ws_url}...")
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("✨ TTSBot との接続が確立されました！音声待機中...")
                    async for message in ws:
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue

                        msg_type = data.get("type")
                        if msg_type == "hello":
                            logger.info(f"[*] サーバー確認: {data.get('message')}")
                        elif msg_type == "game_chat":
                            text = str(data.get("text", "")).strip()
                            user_name = data.get("user_name", "User")
                            if text:
                                self.paste_and_send(text, user_name)
            except (websockets.ConnectionClosed, OSError) as e:
                logger.warning(f"⚠️ 切断されました ({e})。5秒後に再接続します...")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"予期しないエラー: {e}")
                await asyncio.sleep(5)


def main():
    config = load_or_create_config()
    client = GameBridgeClient(config)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\n🛑 終了しました。")


if __name__ == "__main__":
    main()
