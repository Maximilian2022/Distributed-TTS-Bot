import re
from pathlib import Path

# src/version.py
"""
アプリケーションおよびWebUIのバージョン・ビルド番号管理。
デプロイ・更新時に更新することで、ブラウザキャッシュの無効化と画面上でのバージョン確認を可能にします。
"""

BUILD_VERSION = "v2026.10.28-2120"
BUILD_DATE = "2026-10-28 21:20:00 JST"
APP_NAME = "Distributed TTS Discord Bot"
AGENT_VERSION = "3.13.6"  # 最新 Worker Agent バージョン (単一管理元: SSOT)


def sync_agent_py_version() -> bool:
    """リポジトリ内の agent.py の AGENT_VERSION 定義を src/version.py と自動同期する"""
    agent_path = Path(__file__).resolve().parent / "web" / "static" / "agent.py"
    if not agent_path.exists():
        return False
    content = agent_path.read_text(encoding="utf-8")
    new_content = re.sub(
        r'AGENT_VERSION\s*=\s*["\'][^"\']*["\']',
        f'AGENT_VERSION = "{AGENT_VERSION}"',
        content
    )
    if new_content != content:
        agent_path.write_text(new_content, encoding="utf-8")
        return True
    return False



