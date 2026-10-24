#!/usr/bin/env bash
# ==============================================================================
# 🎙️ TTSBot Volunteer Worker 起動 & 自動セットアップスクリプト (Linux / macOS)
# ==============================================================================
# 特徴:
#   - Docker 優先判定 & 未導入時の対話型自動インストール (Y/n)
#   - 音声合成エンジン (VOICEVOX等) の Docker 自動展開
#   - CLI 対話オプション (全体共有 / 自サーバー専用枠, エンジン選択, キャパシティ)
#   - 設定のローカル自動保存 (~/.ttsbot-worker/config.json, デフォルトON)
# ==============================================================================

set -euo pipefail

C_RESET="\033[0m"
C_BOLD="\033[1m"
C_GREEN="\033[32m"
C_YELLOW="\033[33m"
C_RED="\033[31m"
C_CYAN="\033[36m"
C_BLUE="\033[34m"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${HOME}/.ttsbot-worker"
CONFIG_FILE="${WORK_DIR}/config.json"
mkdir -p "${WORK_DIR}"

echo -e "${C_BOLD}${C_CYAN}======================================================${C_RESET}"
echo -e "${C_BOLD}${C_CYAN}  🎙️ TTSBot Volunteer Worker (ボランティア参加ツール)${C_RESET}"
echo -e "${C_BOLD}${C_CYAN}======================================================${C_RESET}"
echo ""

# 1. 既存設定の読み込み判定
RECONFIGURE=0
if [ "${1:-}" = "--config" ] || [ ! -f "${CONFIG_FILE}" ]; then
    RECONFIGURE=1
fi

SERVER_URL="${1:-}"
TOKEN="${2:-}"
EXCLUSIVE_GUILD="${3:-}"

if [ "${RECONFIGURE}" -eq 1 ]; then
    echo -e "${C_BOLD}[1/5] 接続先サーバー設定${C_RESET}"
    if [ -z "${SERVER_URL}" ] || [ "${SERVER_URL}" = "--config" ]; then
        read -rp "接続先 TTSBot サーバーURL (例: wss://tts.example.com/api/worker/ws): " SERVER_URL
    fi
    # URLプロトコル自動補正
    SERVER_URL=$(echo "${SERVER_URL}" | sed -E 's|^https://|wss://|' | sed -E 's|^http://|ws://|')

    if [ -z "${TOKEN}" ]; then
        read -rp "共有トークン (管理者から受領した場合のみ、無ければEnter): " TOKEN
    fi

    echo -e "\n${C_BOLD}[2/5] 提供範囲 (スコープ)${C_RESET}"
    echo "  1) 全体共有プール (全サーバーの読み上げを支援・推奨)"
    echo "  2) 自サーバー専用枠 (自分が参加するサーバーの読み上げのみ処理)"
    read -rp "選択 [1-2] (デフォルト: 1): " SCOPE_ANS
    SCOPE_ANS="${SCOPE_ANS:-1}"

    EXCLUSIVE_GUILD_ID=""
    if [ "${SCOPE_ANS}" = "2" ]; then
        read -rp "専用にする Discord サーバーID (Guild ID): " EXCLUSIVE_GUILD_ID
    fi

    echo -e "\n${C_BOLD}[3/5] 提供する音声合成エンジン${C_RESET}"
    echo "  1) VOICEVOX (四国めたん・ずんだもん等) [推奨]"
    echo "  2) COEIROINK (つくよみちゃん・クロワ等)"
    echo "  3) SHAREVOX (小春音アミ等)"
    echo "  4) すべて (VOICEVOX + COEIROINK + SHAREVOX)"
    read -rp "選択 [1-4] (デフォルト: 1): " ENG_ANS
    ENG_ANS="${ENG_ANS:-1}"

    ENGINES="[\"voicevox\"]"
    case "${ENG_ANS}" in
        1) ENGINES="[\"voicevox\"]" ;;
        2) ENGINES="[\"coeiroink\"]" ;;
        3) ENGINES="[\"sharevox\"]" ;;
        4) ENGINES="[\"voicevox\", \"coeiroink\", \"sharevox\"]" ;;
    esac

    echo -e "\n${C_BOLD}[4/5] 同時処理タスク数 (キャパシティ)${C_RESET}"
    echo "  1) 1件 (低負荷・CPU向け)"
    echo "  2) 2件 (標準・GPU向け 推奨)"
    echo "  3) 3件以上 (ハイスペックマシン向け)"
    read -rp "選択 [1-3] (デフォルト: 2): " CAP_ANS
    CAP_ANS="${CAP_ANS:-2}"

    DEFAULT_NODE_NAME="$(hostname)"
    echo -e "\n${C_BOLD}[5/5] 表示ノード名 (ニックネーム)${C_RESET}"
    read -rp "ノード名 [デフォルト: ${DEFAULT_NODE_NAME}]: " NODE_NAME
    NODE_NAME="${NODE_NAME:-${DEFAULT_NODE_NAME}}"

    # 設定ファイルの保存 (デフォルトON)
    cat <<EOF > "${CONFIG_FILE}"
{
  "server_url": "${SERVER_URL}",
  "token": "${TOKEN}",
  "exclusive_guild_id": "${EXCLUSIVE_GUILD_ID}",
  "engines": ${ENGINES},
  "capacity": ${CAP_ANS},
  "node_name": "${NODE_NAME}",
  "game_priority": true
}
EOF
    echo -e "${C_GREEN}✓ 設定を ${CONFIG_FILE} に保存しました (次回から即起動)${C_RESET}\n"
else
    echo -e "${C_GREEN}✓ 既存の設定 (${CONFIG_FILE}) を読み込みました (再設定は --config)${C_RESET}"
fi

# 2. Docker 環境の検査 & 優先起動
USE_DOCKER=0
if command -v docker &> /dev/null; then
    USE_DOCKER=1
    echo -e "${C_GREEN}✓ Docker 環境が検出されました。Docker を優先利用します。${C_RESET}"
else
    echo -e "${C_YELLOW}❓ Docker が見つかりませんでした。${C_RESET}"
    echo "Docker を導入すると、VOICEVOX などの音声合成エンジンが自動で安定起動します。"
    read -rp "Docker を自動インストールして利用しますか？ (Y/n) [デフォルト: Y]: " DOCKER_INSTALL_ANS
    DOCKER_INSTALL_ANS="${DOCKER_INSTALL_ANS:-Y}"

    if [[ "${DOCKER_INSTALL_ANS}" =~ ^[Yy]$ ]]; then
        echo -e "${C_CYAN}▶ Docker 公式スクリプトを実行してインストール中...${C_RESET}"
        if [ "$EUID" -ne 0 ]; then
            sudo sh -c "curl -fsSL https://get.docker.com | sh"
            sudo usermod -aG docker "$USER" || true
        else
            curl -fsSL https://get.docker.com | sh
        fi
        USE_DOCKER=1
        echo -e "${C_GREEN}✓ Docker のインストールが完了しました。${C_RESET}"
    else
        echo -e "${C_BLUE}ℹ️ ネイティブ Python 環境で起動します。ローカルエンジンを別途起動してください。${C_RESET}"
    fi
fi

# 3. Docker エンジンの自動展開 (Dockerモード時)
if [ "${USE_DOCKER}" -eq 1 ]; then
    COMPOSE_FILE="${WORK_DIR}/docker-compose.yml"
    echo -e "${C_CYAN}▶ 音声合成エンジンのコンテナ設定を生成中...${C_RESET}"

    # GPU 対応判定
    GPU_SNIPPET=""
    if docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi &> /dev/null; then
        echo -e "${C_GREEN}  ✓ NVIDIA GPU が利用可能です。GPU アクセラレーションを有効化します。${C_RESET}"
        GPU_SNIPPET="deploy:\n      resources:\n        reservations:\n          devices:\n            - driver: nvidia\n              count: all\n              capabilities: [gpu]"
    fi

    cat <<EOF > "${COMPOSE_FILE}"
version: '3.8'
services:
  voicevox:
    image: voicevox/voicevox_engine:cpu-latest
    container_name: ttsbot-worker-voicevox
    ports:
      - "127.0.0.1:50021:50021"
    restart: unless-stopped
EOF
    echo -e "${C_CYAN}▶ エンジンコンテナを起動中 (バックグラウンド)...${C_RESET}"
    (cd "${WORK_DIR}" && docker compose up -d) || true
    echo -e "${C_GREEN}✓ 音声合成エンジン (VOICEVOX: 50021) が起動しました。${C_RESET}"
fi

# 4. Python 実行環境の確認 & ライブラリ導入
if ! command -v python3 &>/dev/null; then
    echo -e "${C_RED}❌ Python 3 が見つかりませんでした。Python 3.9 以上をインストールしてください。${C_RESET}"
    exit 1
fi

if ! python3 -c "import aiohttp" &>/dev/null; then
    echo -e "${C_CYAN}▶ 必要なライブラリ (aiohttp) をインストール中...${C_RESET}"
    pip3 install aiohttp || python3 -m pip install aiohttp
fi

# 5. Worker Agent 起動 (config.json からパラメータ展開)
echo -e "${C_GREEN}🚀 ボランティアエージェントを起動します (停止は Ctrl+C)...${C_RESET}\n"

python3 -c "
import json, sys, subprocess

with open('${CONFIG_FILE}', 'r', encoding='utf-8') as f:
    cfg = json.load(f)

args = [
    sys.executable,
    '${SCRIPT_DIR}/volunteer_worker.py',
    '--server-url', cfg.get('server_url', ''),
    '--name', cfg.get('node_name', ''),
    '--capacity', str(cfg.get('capacity', 2))
]

if cfg.get('token'):
    args.extend(['--token', cfg.get('token')])
if cfg.get('exclusive_guild_id'):
    args.extend(['--exclusive-guild-id', cfg.get('exclusive_guild_id')])
if cfg.get('game_priority', True):
    args.append('--game-priority')

sys.exit(subprocess.call(args))
"
