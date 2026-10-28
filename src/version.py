# src/version.py
"""
アプリケーションおよびWebUIのバージョン・ビルド番号管理。
デプロイ・更新時に更新することで、ブラウザキャッシュの無効化と画面上でのバージョン確認を可能にします。
"""

BUILD_VERSION = "v2026.10.28-1615"
BUILD_DATE = "2026-10-28 16:15:00 JST"
APP_NAME = "Distributed TTS Discord Bot"
AGENT_VERSION = "3.13.2"  # 最新 Worker Agent バージョン (単一管理元 / src/web/static/agent.py と必ず一致させること)


