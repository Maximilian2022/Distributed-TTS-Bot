#!/usr/bin/env bash
# ==============================================================================
# 🎙️ TTSBot Volunteer Worker 起動スクリプト (Linux / macOS)
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVER_URL="${1:-${TTSBOT_SERVER_URL:-}}"
TOKEN="${2:-${TTSBOT_TOKEN:-}}"
NODE_NAME="${3:-${TTSBOT_NODE_NAME:-$(hostname)}}"

echo "======================================================"
echo "  🎙️ TTSBot Volunteer Worker (ボランティア参加ツール)"
echo "======================================================"

if [ -z "$SERVER_URL" ]; then
    echo "接続先の TTSBot サーバーURLを入力してください"
    echo "例: wss://your-domain.com/api/worker/ws  または  ws://192.168.1.100:8080/api/worker/ws"
    read -rp "Server URL: " SERVER_URL
fi

if [ -z "$SERVER_URL" ]; then
    echo "❌ 接続先サーバーURLが入力されませんでした。"
    exit 1
fi

# wss / ws への変換
SERVER_URL="${SERVER_URL/https:\/\//wss:\/\/}"
SERVER_URL="${SERVER_URL/http:\/\//ws:\/\/}"


if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 が見つかりませんでした。Python 3.9 以上をインストールしてください。"
    exit 1
fi

if [ -z "$TOKEN" ]; then
    read -rp "管理者から受け取った共有トークンを入力してください: " TOKEN
fi

echo ""
echo "▶ 接続先サーバー : ${SERVER_URL}"
echo "▶ ノード名       : ${NODE_NAME}"
echo ""

if ! python3 -c "import aiohttp" &>/dev/null; then
    echo "▶ 必要なライブラリ (aiohttp) をインストール中..."
    pip3 install aiohttp
fi

echo "▶ ボランティアエージェントを起動します..."
exec python3 "${SCRIPT_DIR}/volunteer_worker.py" \
    --server-url "${SERVER_URL}" \
    --token "${TOKEN}" \
    --name "${NODE_NAME}"
