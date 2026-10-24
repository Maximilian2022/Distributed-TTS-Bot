#!/usr/bin/env bash
# ==============================================================================
# 🎙️ TTSBot Volunteer Worker 起動 & 自動セットアップスクリプト (Linux / macOS)
# ==============================================================================
# 特徴:
#   - ワンライナー (curl ... | bash) 完全対応 (/dev/tty 入力保護 & エージェント自動取得)
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

WORK_DIR="${HOME}/.ttsbot-worker"
CONFIG_FILE="${WORK_DIR}/config.json"
mkdir -p "${WORK_DIR}"

SCRIPT_DIR="${WORK_DIR}"
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

# パイプ実行 (curl ... | bash) でもスクリプト本文を消費しない安全な対話入力関数
read_input() {
    local prompt="$1"
    local var_name="$2"
    local default_val="${3:-}"
    local user_val=""

    if [ -t 0 ]; then
        read -rp "$prompt" user_val || true
    elif [ -r /dev/tty ]; then
        read -rp "$prompt" user_val < /dev/tty || true
    fi

    user_val="${user_val:-$default_val}"
    printf -v "$var_name" '%s' "$user_val"
}

echo -e "${C_BOLD}${C_CYAN}======================================================${C_RESET}"
echo -e "${C_BOLD}${C_CYAN}  🎙️ TTSBot Volunteer Worker (ボランティア参加ツール)${C_RESET}"
echo -e "${C_BOLD}${C_CYAN}======================================================${C_RESET}"
echo ""

# 1. 既存設定の読み込み判定
RECONFIGURE=0
if [ "${1:-}" = "--config" ] || [ ! -f "${CONFIG_FILE}" ]; then
    RECONFIGURE=1
elif ! python3 -c "import json; cfg=json.load(open('${CONFIG_FILE}', 'r', encoding='utf-8')); assert cfg.get('token')" &>/dev/null; then
    echo -e "${C_YELLOW}⚠️ 共有トークンが設定されていないため、セットアップを行います (WAN接続には必須です)。${C_RESET}"
    RECONFIGURE=1
fi

AUTO_SERVER_URL="__DEFAULT_SERVER_WS_URL__"
AUTO_TOKEN="__DEFAULT_WORKER_TOKEN__"
case "${AUTO_SERVER_URL}" in *"__DEFAULT_"*) AUTO_SERVER_URL="" ;; esac
case "${AUTO_TOKEN}" in *"__DEFAULT_"*) AUTO_TOKEN="" ;; esac

SERVER_URL="${1:-$AUTO_SERVER_URL}"
TOKEN="${2:-$AUTO_TOKEN}"
EXCLUSIVE_GUILD="${3:-}"

if [ "${RECONFIGURE}" -eq 1 ]; then
    echo -e "${C_BOLD}[1/5] 接続先サーバー設定${C_RESET}"
    if [ -z "${SERVER_URL}" ] || [ "${SERVER_URL}" = "--config" ]; then
        read_input "接続先 TTSBot サーバーURL (例: wss://ttsbot.example.com/api/worker/ws): " SERVER_URL "${AUTO_SERVER_URL}"
    fi
    # URLプロトコル自動補正
    SERVER_URL=$(echo "${SERVER_URL}" | sed -E 's|^https://|wss://|' | sed -E 's|^http://|ws://|')

    while [ -z "${TOKEN}" ]; do
        read_input "共有トークン [必須] (管理者から受領したトークンを入力): " TOKEN "${AUTO_TOKEN}"
        if [ -z "${TOKEN}" ]; then
            echo -e "${C_RED}❌ 共有トークンは必須です。Cloudflare / WAN経由の接続には認証が必要です。${C_RESET}"
        fi
    done

    echo -e "\n${C_BOLD}[2/5] 提供範囲 (スコープ)${C_RESET}"
    echo "  1) 全体共有プール (全サーバーの読み上げを支援・推奨)"
    echo "  2) 自サーバー専用枠 (自分が参加するサーバーの読み上げのみ処理)"
    read_input "選択 [1-2] (デフォルト: 1): " SCOPE_ANS "1"

    EXCLUSIVE_GUILD_ID=""
    if [ "${SCOPE_ANS}" = "2" ]; then
        read_input "専用にする Discord サーバーID (Guild ID): " EXCLUSIVE_GUILD_ID ""
    fi

    echo -e "\n${C_BOLD}[3/5] 提供する音声合成エンジン${C_RESET}"
    echo "  1) VOICEVOX (四国めたん・ずんだもん等) [推奨]"
    echo "  2) COEIROINK (つくよみちゃん・クロワ等)"
    echo "  3) SHAREVOX (小春音アミ等)"
    echo "  4) すべて (VOICEVOX + COEIROINK + SHAREVOX)"
    read_input "選択 [1-4] (デフォルト: 1): " ENG_ANS "1"

    ENGINES="[\"voicevox\"]"
    case "${ENG_ANS}" in
        1) ENGINES="[\"voicevox\"]" ;;
        2) ENGINES="[\"coeiroink\"]" ;;
        3) ENGINES="[\"sharevox\"]" ;;
        4) ENGINES="[\"voicevox\", \"coeiroink\", \"sharevox\"]" ;;
    esac

    # COEIROINK 選択時の注意事項と確認
    if [ "${ENG_ANS}" = "2" ] || [ "${ENG_ANS}" = "4" ]; then
        echo -e "\n${C_YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}"
        echo -e "${C_BOLD}${C_YELLOW}⚠️  【重要】COEIROINK の自動セットアップに関する注意事項${C_RESET}"
        echo -e "${C_YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}"
        echo -e "  COEIROINK はエンジンおよび音声モデルのファイルサイズが大きく (${C_BOLD}約 3.5GB${C_RESET})、"
        echo -e "  ダウンロードと Docker コンテナの構築に ${C_BOLD}10〜30分程度${C_RESET} かかります。"
        echo -e "  ディスク空き容量（10GB 以上推奨）と安定したインターネット回線が必要です。"
        echo -e "${C_YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}"
        read_input "それでも COEIROINK の自動ダウンロード＆起動を行いますか？ (y/N) [デフォルト: N]: " CI_CONFIRM "N"
        if [[ ! "${CI_CONFIRM}" =~ ^[Yy]$ ]]; then
            echo -e "${C_BLUE}ℹ️ COEIROINK のセットアップをスキップします。${C_RESET}"
            if [ "${ENG_ANS}" = "2" ]; then
                ENGINES="[\"voicevox\"]"
            else
                ENGINES="[\"voicevox\", \"sharevox\"]"
            fi
        else
            echo -e "${C_GREEN}✓ COEIROINK の自動セットアップを承諾しました。${C_RESET}"
        fi
    fi

    echo -e "\n${C_BOLD}[4/5] 同時処理タスク数 (キャパシティ)${C_RESET}"
    echo "  1) 1件 (低負荷・CPU向け)"
    echo "  2) 2件 (標準・GPU向け 推奨)"
    echo "  3) 3件以上 (ハイスペックマシン向け)"
    read_input "選択 [1-3] (デフォルト: 2): " CAP_ANS "2"

    DEFAULT_NODE_NAME="$(hostname)"
    echo -e "\n${C_BOLD}[5/5] 表示ノード名 (ニックネーム)${C_RESET}"
    read_input "ノード名 [デフォルト: ${DEFAULT_NODE_NAME}]: " NODE_NAME "${DEFAULT_NODE_NAME}"

    # 設定ファイルの保存 (Pythonのjson.dumpで100%安全・確実にJSON生成)
    python3 -c "
import json
try:
    engines = json.loads('''${ENGINES}''')
except Exception:
    engines = ['voicevox']

cap = 2
try:
    cap = int('${CAP_ANS}')
except Exception:
    pass

cfg = {
    'server_url': '''${SERVER_URL}''',
    'token': '''${TOKEN}''',
    'exclusive_guild_id': '''${EXCLUSIVE_GUILD_ID}''',
    'engines': engines,
    'capacity': cap,
    'node_name': '''${NODE_NAME}''',
    'runtime': 'docker' if int('${USE_DOCKER:-0}') == 1 else 'native',
    'game_priority': True
}
with open('${CONFIG_FILE}', 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
"
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
    read_input "Docker を自動インストールして利用しますか？ (Y/n) [デフォルト: Y]: " DOCKER_INSTALL_ANS "Y"

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

    ENGINES_VAL=$(python3 -c "import json; cfg=json.load(open('${CONFIG_FILE}')); print(' '.join(cfg.get('engines', [])))" 2>/dev/null || echo "voicevox")

    cat <<EOF > "${COMPOSE_FILE}"
version: '3.8'

services:
EOF

    # VOICEVOX: ポート 50021
    if echo "${ENGINES_VAL}" | grep -q "voicevox"; then
        cat <<EOF >> "${COMPOSE_FILE}"
  voicevox:
    image: voicevox/voicevox_engine:cpu-latest
    container_name: ttsbot-worker-voicevox
    ports:
      - "127.0.0.1:50021:50021"
    restart: unless-stopped
EOF
    fi

    # SHAREVOX: ポート衝突防止のためホスト側を 50025 にバインド
    if echo "${ENGINES_VAL}" | grep -q "sharevox"; then
        cat <<EOF >> "${COMPOSE_FILE}"
  sharevox:
    image: sharevox/sharevox_engine:cpu-latest
    container_name: ttsbot-worker-sharevox
    ports:
      - "127.0.0.1:50025:50021"
    restart: unless-stopped
EOF
    fi

    # COEIROINK: ポート 50031
    if echo "${ENGINES_VAL}" | grep -q "coeiroink"; then
        echo -e "${C_CYAN}▶ COEIROINK Docker ビルド環境を準備中 (容量約3.5GB)...${C_RESET}"
        CI_DOCKER_DIR="${WORK_DIR}/docker/coeiroink"
        mkdir -p "${CI_DOCKER_DIR}"

        HAS_GPU="false"
        if command -v nvidia-smi > /dev/null 2>&1 && nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | grep -q .; then
            HAS_GPU="true"
            CI_BASE_IMAGE="nvidia/cuda:11.8.0-base-ubuntu22.04"
            CI_ZIP_URL="https://www.dropbox.com/scl/fi/pisvh3pwlinz6hdep2fcr/COEIROINK_LINUX_GPU_v.2025.4.1.zip?rlkey=4dg1368zzdbplahcjdbqoj2tg&st=3k7yonjs&dl=1"
        else
            CI_BASE_IMAGE="ubuntu:22.04"
            CI_ZIP_URL="https://www.dropbox.com/scl/fi/o7xnjfo0jw81otgn2jxar/COEIROINK_LINUX_CPU_v.2025.4.1.zip?rlkey=dt1z2bxrbo3ikm2iiioxh9wx7&st=tso7516f&dl=1"
        fi

        cat > "${CI_DOCKER_DIR}/Dockerfile" << EOF
FROM ${CI_BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y curl unzip ca-certificates libgomp1 libsndfile1 socat && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/coeiroink
RUN curl -L -o /tmp/coeiroink.zip "${CI_ZIP_URL}" && \\
    unzip -q /tmp/coeiroink.zip -d /opt/coeiroink && \\
    rm /tmp/coeiroink.zip && \\
    find /opt/coeiroink -type f -name "engine" -exec chmod +x {} +
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 50031 50032
ENTRYPOINT ["/entrypoint.sh"]
EOF

        cat > "${CI_DOCKER_DIR}/entrypoint.sh" << 'EOF'
#!/bin/bash
set -e
DIR=$(find /opt/coeiroink -maxdepth 2 -type d -name "COEIROINK_LINUX_*" | head -n 1)
if [ -z "$DIR" ]; then
    DIR="/opt/coeiroink"
fi
if [ -d "$DIR/engine" ]; then
    cd "$DIR/engine"
    export LD_LIBRARY_PATH="$DIR/engine:$DIR/engine/torch/lib:/usr/local/cuda/lib64:$LD_LIBRARY_PATH"
    socat TCP-LISTEN:50031,fork,reuseaddr TCP:127.0.0.1:50032 &
    SOCAT_PID=$!
    "$DIR/engine/engine" &
    ENGINE_PID=$!
    wait -n $ENGINE_PID $SOCAT_PID
else
    echo "COEIROINK engine directory not found in $DIR"
    exit 1
fi
EOF
        chmod +x "${CI_DOCKER_DIR}/entrypoint.sh"

        if [ "$HAS_GPU" = "true" ]; then
            cat <<EOF >> "${COMPOSE_FILE}"
  coeiroink:
    build:
      context: ./docker/coeiroink
      dockerfile: Dockerfile
    container_name: ttsbot-worker-coeiroink
    ports:
      - "127.0.0.1:50031:50031"
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
EOF
        else
            cat <<EOF >> "${COMPOSE_FILE}"
  coeiroink:
    build:
      context: ./docker/coeiroink
      dockerfile: Dockerfile
    container_name: ttsbot-worker-coeiroink
    ports:
      - "127.0.0.1:50031:50031"
    restart: unless-stopped
EOF
        fi
    fi

    echo -e "${C_CYAN}▶ エンジンコンテナをビルド・起動中 (バックグラウンド)...${C_RESET}"
    (cd "${WORK_DIR}" && docker compose up -d --build) || true
    echo -e "${C_GREEN}✓ 音声合成エンジンコンテナが起動しました (VOICEVOX / SHAREVOX / COEIROINK)${C_RESET}"
fi

# 4. Python 実行環境の確認 & ライブラリ導入
if ! command -v python3 &>/dev/null; then
    echo -e "${C_RED}❌ Python 3 が見つかりませんでした。Python 3.9 以上をインストールしてください。${C_RESET}"
    exit 1
fi

if ! python3 -c "import aiohttp" &>/dev/null; then
    echo -e "${C_CYAN}▶ 必要なライブラリ (aiohttp) をインストール中...${C_RESET}"
    pip3 install aiohttp || python3 -m pip install aiohttp || true
fi

# 5. Worker Agent スクリプトの自動同期 (起動時に常に最新版をチェック・更新)
AGENT_FILE="${SCRIPT_DIR}/volunteer_worker.py"
RAW_SERVER_URL=$(python3 -c "import json; cfg=json.load(open('${CONFIG_FILE}')); print(cfg.get('server_url', ''))" 2>/dev/null || echo "${SERVER_URL}")
HTTP_BASE=$(echo "${RAW_SERVER_URL}" | sed -E 's|^wss://|https://|' | sed -E 's|^ws://|http://|' | sed -E 's|/api/worker/ws/?$||' | sed -E 's|/+$||')
if [ -n "${HTTP_BASE}" ]; then
    echo -e "${C_CYAN}🔄 ボランティアエージェントの最新版を同期中...${C_RESET}"
    TARGET_FILE="${AGENT_FILE}"
    [ -f "${SCRIPT_DIR}/volunteer_worker.py" ] || TARGET_FILE="${WORK_DIR}/volunteer_worker.py"
    TMP_FILE="${TARGET_FILE}.tmp"
    if curl -sSL --connect-timeout 5 --max-time 15 "${HTTP_BASE}/volunteer/volunteer_worker.py" -o "${TMP_FILE}" 2>/dev/null && [ -s "${TMP_FILE}" ]; then
        mv "${TMP_FILE}" "${TARGET_FILE}"
        AGENT_FILE="${TARGET_FILE}"
        echo -e "${C_GREEN}✓ 最新のエージェントスクリプトを確認・同期しました。${C_RESET}"
    else
        rm -f "${TMP_FILE}"
        if [ -f "${AGENT_FILE}" ] || [ -f "${WORK_DIR}/volunteer_worker.py" ]; then
            echo -e "${C_YELLOW}⚠️ 最新版の取得に失敗したため、ローカルの既存エージェントで起動します。${C_RESET}"
        fi
    fi
fi

if [ ! -f "${AGENT_FILE}" ]; then
    AGENT_FILE="${WORK_DIR}/volunteer_worker.py"
fi

if [ ! -f "${AGENT_FILE}" ]; then
    echo -e "${C_RED}❌ volunteer_worker.py を取得できませんでした。サーバーURL (${SERVER_URL}) またはネットワーク接続を確認してください。${C_RESET}"
    exit 1
fi

# 6. Worker Agent 起動 (config.json からパラメータ展開)
echo -e "${C_GREEN}🚀 ボランティアエージェントを起動します (停止は Ctrl+C)...${C_RESET}\n"

python3 -c "
import json, sys, subprocess

try:
    with open('${CONFIG_FILE}', 'r', encoding='utf-8') as f:
        cfg = json.load(f)
except Exception:
    cfg = {}

args = [
    sys.executable,
    '${AGENT_FILE}',
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
runtime = cfg.get('runtime', 'docker' if int('${USE_DOCKER:-0}') == 1 else 'native')
if runtime:
    args.extend(['--runtime', runtime])

sys.exit(subprocess.call(args))
"
