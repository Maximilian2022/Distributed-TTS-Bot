#!/usr/bin/env bash
# ==============================================================================
# TTSBot Manager Slave (Standby) One-Liner Auto Setup Script
# ==============================================================================
# 使い方:
#   curl -sSL https://<あなたのTTSBotドメイン>/setup-standby.sh | bash
#   または引数指定:
#   curl -sSL https://.../setup-standby.sh | bash -s -- https://<MASTER_URL> <HA_SECRET> [TUNNEL_TOKEN]
# ==============================================================================

set -e

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
RESET="\033[0m"

echo -e "${BOLD}${CYAN}======================================================${RESET}"
echo -e "${BOLD}${CYAN}  🎙️ TTSBot Manager Slave (Standby) 自動セットアップ${RESET}"
echo -e "${BOLD}${CYAN}======================================================${RESET}"

# 1. 引数または環境変数の取得
MASTER_URL="${1:-${TTSBOT_MASTER_URL:-}}"
HA_SECRET="${2:-${TTSBOT_HA_SECRET:-}}"
TUNNEL_TOKEN="${3:-${CLOUDFLARE_TUNNEL_TOKEN:-}}"

# 2. 未指定時に対話入力 (パイプ実行時の /dev/tty 読み取り対応)
if [ -z "$MASTER_URL" ]; then
    if [ -t 0 ]; then
        echo -e "${YELLOW}Master (主系) サーバーのベースURLを入力してください:${RESET}"
        read -rp "Master URL (例: https://ttsbot.example.com): " MASTER_URL
    elif [ -r /dev/tty ]; then
        echo -e "${YELLOW}Master (主系) サーバーのベースURLを入力してください:${RESET}" > /dev/tty
        read -rp "Master URL (例: https://ttsbot.example.com): " MASTER_URL < /dev/tty || true
    fi
fi

# 末尾スラッシュ除去
MASTER_URL=$(echo "$MASTER_URL" | sed 's:/*$::')

if [ -z "$MASTER_URL" ]; then
    echo -e "${RED}エラー: Master (主系) サーバーのURLが指定されていません。${RESET}"
    echo "使用例: curl -sSL https://<Master-URL>/setup-standby.sh | sudo bash -s -- https://<Master-URL> <HA_SECRET>"
    exit 1
fi

if [ -z "$HA_SECRET" ]; then
    if [ -t 0 ]; then
        echo -e "${YELLOW}HA 共有認証シークレットキー (TTSBOT_HA_SECRET) を入力してください:${RESET}"
        read -rp "HA Secret: " HA_SECRET
    elif [ -r /dev/tty ]; then
        echo -e "${YELLOW}HA 共有認証シークレットキー (TTSBOT_HA_SECRET) を入力してください:${RESET}" > /dev/tty
        read -rp "HA Secret: " HA_SECRET < /dev/tty || true
    fi
fi

if [ -z "$HA_SECRET" ]; then
    echo -e "${RED}エラー: HA 共有認証シークレットキー (TTSBOT_HA_SECRET) が指定されていません。${RESET}"
    echo "使用例: curl -sSL https://<Master-URL>/setup-standby.sh | sudo bash -s -- https://<Master-URL> <HA_SECRET>"
    exit 1
fi

# 3. Docker 稼働チェック
echo -e "\n${BOLD}[1/4] Docker 環境の確認中...${RESET}"
if ! command -v docker &> /dev/null; then
    echo -e "${RED}エラー: docker コマンドが見つかりません。${RESET}"
    echo "Docker をインストールしてから再度実行してください: https://get.docker.com"
    exit 1
fi

DOCKER_COMPOSE_CMD=""
if docker compose version &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker compose"
else
    echo -e "${RED}エラー: docker compose が利用できません。${RESET}"
    exit 1
fi
echo -e "  ✓ Docker: ${GREEN}OK${RESET}"
echo -e "  ✓ Compose: ${GREEN}${DOCKER_COMPOSE_CMD}${RESET}"

# 4. Master への疎通テスト
echo -e "\n${BOLD}[2/4] Master サーバー (${MASTER_URL}) への疎通確認中...${RESET}"
HEALTH_JSON=$(curl -s --connect-timeout 5 --max-time 10 "${MASTER_URL}/health" 2>/dev/null || true)
if echo "$HEALTH_JSON" | grep -q '"status"'; then
    echo -e "  ✓ Master /health 応答: ${GREEN}正常 (200 OK)${RESET}"
else
    echo -e "  ${YELLOW}⚠️ 警告: Master (${MASTER_URL}/health) から応答がありません。${RESET}"
    echo -e "  URL が正しいか、Master が起動しているか確認してください（セットアップは継続します）。"
fi

# 5. インストール先ディレクトリの準備
INSTALL_DIR="${HOME}/ttsbot-slave"
echo -e "\n${BOLD}[3/4] Slave 設定・ディレクトリのセットアップ (${INSTALL_DIR})...${RESET}"
mkdir -p "${INSTALL_DIR}/data" "${INSTALL_DIR}/logs"

cd "${INSTALL_DIR}"

# リポジトリが未存在なら clone または Master から直接取得
if [ ! -f "docker-compose.yml" ]; then
    if command -v git &> /dev/null; then
        echo "  リポジトリを取得中 (git clone)..."
        git clone https://github.com/Maximilian2022/ttsbot-oss.git . 2>/dev/null || true
    fi
    # git clone が失敗または未インストールの場合は Master から直接ダウンロード補完
    if [ ! -f "docker-compose.yml" ]; then
        echo "  Master から docker-compose.yml を取得中..."
        curl -sSL --connect-timeout 5 --max-time 15 "${MASTER_URL}/docker-compose.yml" -o docker-compose.yml 2>/dev/null || true
    fi
fi

# .env ファイルの生成 / 更新
ENV_FILE="${INSTALL_DIR}/.env"
echo "  .env 設定ファイルを生成中..."

# 既存の設定値をバックアップ
touch "$ENV_FILE"

cat <<EOF > "$ENV_FILE"
# ==============================================================================
# TTSBot Slave (Standby) 環境設定
# 生成日時: $(date '+%Y-%m-%d %H:%M:%S')
# ==============================================================================

# HA ロール設定
TTSBOT_HA_ROLE=slave
TTSBOT_MASTER_URL=${MASTER_URL}
TTSBOT_HA_SECRET=${HA_SECRET}
TTSBOT_CHECK_INTERVAL=5
TTSBOT_FAIL_THRESHOLD=6
TTSBOT_SYNC_INTERVAL=60

# Web & ポート設定
HOST_WEB_PORT=8088
WEB_PORT=8080

# Cloudflare Tunnel (任意)
CLOUDFLARE_TUNNEL_TOKEN=${TUNNEL_TOKEN}

EOF

echo -e "  ✓ .env ファイル作成完了: ${GREEN}${ENV_FILE}${RESET}"

# 6. Standby Watchdog コンテナの起動 (--profile standby 指定)
echo -e "\n${BOLD}[4/4] Slave Standby Watchdog を起動中...${RESET}"
${DOCKER_COMPOSE_CMD} --profile standby up -d standby-watchdog

echo -e "\n${BOLD}${GREEN}======================================================${RESET}"
echo -e "${BOLD}${GREEN}  🎉 TTSBot Slave (Standby) のセットアップが完了しました！${RESET}"
echo -e "${BOLD}${GREEN}======================================================${RESET}"
echo -e "  ・動作モード   : ${CYAN}Slave (Standby 待機・自動同期)${RESET}"
echo -e "  ・監視対象     : ${CYAN}${MASTER_URL}${RESET}"
echo -e "  ・管理コマンド :"
echo -e "     - 状態・ログ確認 : ${BOLD}cd ${INSTALL_DIR} && ${DOCKER_COMPOSE_CMD} --profile standby logs -f standby-watchdog${RESET}"
echo -e "     - 停止           : ${BOLD}cd ${INSTALL_DIR} && ${DOCKER_COMPOSE_CMD} --profile standby stop standby-watchdog${RESET}"
echo -e "     - 再開           : ${BOLD}cd ${INSTALL_DIR} && ${DOCKER_COMPOSE_CMD} --profile standby up -d standby-watchdog${RESET}"
echo ""
echo -e "💡 Master がダウンした際は自動的に Active に昇格し、Master が復旧すると自動的に Standby に戻ります。"
echo ""
