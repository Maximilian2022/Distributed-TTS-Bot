#!/usr/bin/env bash
# ==============================================================================
# TTSBot Blue-Green ゼロダウンタイム・ローリングアップデートスクリプト
# ==============================================================================
# 使い方:
#   ./scripts/rolling_update.sh             # 通常の高速切り替え (コード変更反映)
#   ./scripts/rolling_update.sh --build     # 新イメージをビルドしてから切り替え
#   ./scripts/rolling_update.sh --no-cache  # キャッシュ破棄完全リビルドして切り替え
#   ./scripts/rolling_update.sh --clean     # 切り替え後に不要な古いイメージを削除
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
RESET="\033[0m"

log_info() { echo -e "${CYAN}[INFO]${RESET} $1"; }
log_ok()   { echo -e "${GREEN}[OK]${RESET} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${RESET} $1"; }
log_err()  { echo -e "${RED}[ERROR]${RESET} $1"; }

DO_BUILD=0
NO_CACHE=0
DO_CLEAN=0

for arg in "$@"; do
  case "$arg" in
    --build)
      DO_BUILD=1
      ;;
    --no-cache)
      DO_BUILD=1
      NO_CACHE=1
      ;;
    --clean)
      DO_CLEAN=1
      ;;
    -h|--help)
      echo "使用方法: $0 [--build] [--no-cache] [--clean]"
      exit 0
      ;;
    *)
      log_warn "未知の引数: $arg (無視されます)"
      ;;
  esac
done

echo -e "${BOLD}${CYAN}======================================================${RESET}"
echo -e "${BOLD}${CYAN}   TTSBot Blue-Green ローリングアップデート開始${RESET}"
echo -e "${BOLD}${CYAN}======================================================${RESET}"

# 1. 現在稼働中のコンテナを判定
ACTIVE_SLOT=""
OLD_LEGACY=0

if docker ps --format '{{.Names}}' | grep -q '^ttsbot-blue$'; then
  ACTIVE_SLOT="blue"
elif docker ps --format '{{.Names}}' | grep -q '^ttsbot-green$'; then
  ACTIVE_SLOT="green"
elif docker ps --format '{{.Names}}' | grep -q '^discord-tts-bot$'; then
  ACTIVE_SLOT="legacy"
  OLD_LEGACY=1
else
  log_warn "現在稼働中の TTSBot コンテナが見つかりません。初回起動として ttsbot-blue を立ち上げます。"
  ACTIVE_SLOT="none"
fi

if [ "$ACTIVE_SLOT" = "blue" ]; then
  CURRENT_CONTAINER="ttsbot-blue"
  TARGET_SLOT="green"
  TARGET_CONTAINER="ttsbot-green"
  TARGET_SERVICE="ttsbot-green"
elif [ "$ACTIVE_SLOT" = "green" ]; then
  CURRENT_CONTAINER="ttsbot-green"
  TARGET_SLOT="blue"
  TARGET_CONTAINER="ttsbot-blue"
  TARGET_SERVICE="ttsbot-blue"
elif [ "$ACTIVE_SLOT" = "legacy" ]; then
  CURRENT_CONTAINER="discord-tts-bot"
  TARGET_SLOT="blue"
  TARGET_CONTAINER="ttsbot-blue"
  TARGET_SERVICE="ttsbot-blue"
else
  CURRENT_CONTAINER=""
  TARGET_SLOT="blue"
  TARGET_CONTAINER="ttsbot-blue"
  TARGET_SERVICE="ttsbot-blue"
fi

log_info "現在稼働中: ${BOLD}${CURRENT_CONTAINER:-なし}${RESET}"
log_info "切り替え先: ${BOLD}${TARGET_CONTAINER}${RESET} (スロット: ${TARGET_SLOT})"

# 2. ビルドフェーズ (オプション)
if [ "$DO_BUILD" -eq 1 ]; then
  log_info "裏で新イメージのビルドを開始します (※稼働中のBot通話は影響を受けません)..."
  BUILD_CMD=(docker compose build)
  if [ "$NO_CACHE" -eq 1 ]; then
    BUILD_CMD+=(--no-cache)
  fi
  BUILD_CMD+=("$TARGET_SERVICE")
  
  "${BUILD_CMD[@]}"
  log_ok "イメージビルド完了"
fi

# 3. Nginx ルーター設定ファイルの準備
mkdir -p "$PROJECT_ROOT/docker/nginx"
NGINX_CONF="$PROJECT_ROOT/docker/nginx/default.conf"

cat <<EOF > "$NGINX_CONF"
upstream ttsbot_backend {
    server ${TARGET_CONTAINER}:8080;
}

server {
    listen 8080;
    server_name _;

    client_max_body_size 100M;

    location / {
        proxy_pass http://ttsbot_backend;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        proxy_connect_timeout 5s;
    }
}
EOF

# 4. 新スロットを STANDBY モードで起動 (一時シグナルファイル方式: 環境変数の焼き付きを完全根絶)
log_info "一時スタンバイトリガー (/app/data/standby_trigger) を作成中..."
if [ -n "$CURRENT_CONTAINER" ] && docker ps --format '{{.Names}}' | grep -q "^${CURRENT_CONTAINER}$"; then
  docker exec "$CURRENT_CONTAINER" touch /app/data/standby_trigger 2>/dev/null || true
else
  docker run --rm -v bot_data:/data alpine touch /data/standby_trigger 2>/dev/null || true
fi

log_info "裏で新コンテナ ${BOLD}${TARGET_CONTAINER}${RESET} をスタンバイ起動します..."
docker compose up -d "$TARGET_SERVICE"

# 新コンテナ内の古いアクティベートフラグを念のため排除
docker exec "$TARGET_CONTAINER" rm -f /tmp/ttsbot_activate 2>/dev/null || true

# 5. ウォームアップ完了 (READY) 待機
log_info "ウォームアップ完了 (/tmp/ttsbot_ready) を待機中..."
WAIT_TIMEOUT=45
ELAPSED=0
READY=0

while [ "$ELAPSED" -lt "$WAIT_TIMEOUT" ]; do
  if docker exec "$TARGET_CONTAINER" test -f /tmp/ttsbot_ready 2>/dev/null || docker exec "$TARGET_CONTAINER" test -f /tmp/ttsbot_active 2>/dev/null; then
    READY=1
    break
  fi
  
  # コンテナが異常終了していないかチェック
  if ! docker ps --format '{{.Names}}' | grep -q "^${TARGET_CONTAINER}$"; then
    log_err "新コンテナ ${TARGET_CONTAINER} が起動中に停止しました！ログを出力します:"
    docker logs --tail 30 "$TARGET_CONTAINER" || true
    exit 1
  fi
  
  sleep 1
  ELAPSED=$((ELAPSED + 1))
done

if [ "$READY" -ne 1 ]; then
  log_err "ウォームアップ待機がタイムアウト (${WAIT_TIMEOUT}秒) しました！"
  log_warn "旧コンテナは保護されているため通話は継続します。新コンテナのログを出力します:"
  docker logs --tail 30 "$TARGET_CONTAINER" || true
  docker stop "$TARGET_CONTAINER" || true
  exit 1
fi

log_ok "新コンテナ ${TARGET_CONTAINER} のウォームアップ完了を検知！"

# 6. 高速ハンドオフ (切り替え) 開始
log_info "⚡ 高速ハンドオフを開始します (VC瞬断目標: 1〜2秒)..."

# 6-1. Nginx ルーターの起動 / リロード
if docker ps --format '{{.Names}}' | grep -q '^ttsbot-router$'; then
  docker exec ttsbot-router nginx -s reload || docker restart ttsbot-router
else
  # 旧レガシーコンテナがホストポート8088を占有している場合は後で起動
  if [ "$OLD_LEGACY" -ne 1 ]; then
    docker compose up -d router
  fi
fi

# 6-2. 旧コンテナの安全停止 (Graceful Drain: 発話終了待機 & キュー退避 & VC切断)
if [ -n "$CURRENT_CONTAINER" ]; then
  log_info "旧コンテナ ${CURRENT_CONTAINER} に安全停止シグナルを送信中..."
  docker stop -t 15 "$CURRENT_CONTAINER"
  log_ok "旧コンテナ停止完了"
  
  # 旧レガシーコンテナだった場合は削除してルーターを立ち上げる
  if [ "$OLD_LEGACY" -eq 1 ]; then
    docker rm "$CURRENT_CONTAINER" || true
    log_info "ルーターコンテナ ttsbot-router を起動します..."
    docker compose up -d router
  fi
fi

# 6-3. 新コンテナのアクティベート (即時Gateway接続・VC復帰・キュー復元)
log_info "新コンテナ ${TARGET_CONTAINER} にアクティベートシグナルを送信します..."
docker exec "$TARGET_CONTAINER" rm -f /app/data/standby_trigger 2>/dev/null || true
docker exec "$TARGET_CONTAINER" touch /tmp/ttsbot_activate 2>/dev/null || docker kill -s USR1 "$TARGET_CONTAINER"


# 7. 復帰確認 (最大10秒)
log_info "Gateway接続 & VC復元を監視中..."
RESTORED=0
for i in {1..10}; do
  if docker exec "$TARGET_CONTAINER" test -f /tmp/ttsbot_active 2>/dev/null; then
    RESTORED=1
    break
  fi
  sleep 1
done

if [ "$RESTORED" -eq 1 ]; then
  log_ok "✨ 新コンテナ ${TARGET_CONTAINER} のアクティブ化が正常完了しました！"
  # 新コンテナをアクティブスロットとして永続化 (次回ホスト再起動時のスタンバイトラップ防止)
  docker exec "$TARGET_CONTAINER" bash -c "echo 'ubuntu:$TARGET_SLOT' > /app/data/active_slot.txt" 2>/dev/null || true
else
  log_warn "アクティブフラグの確認に時間がかかっていますが、コンテナは稼働中です。"
  docker exec "$TARGET_CONTAINER" bash -c "echo 'ubuntu:$TARGET_SLOT' > /app/data/active_slot.txt" 2>/dev/null || true
fi

# 8. 不要イメージのゴミ掃除 (オプション)
if [ "$DO_CLEAN" -eq 1 ]; then
  log_info "不要なダングリングイメージ・古いキャッシュを掃除中..."
  docker image prune -f || true
  log_ok "クリーンアップ完了"
fi

echo ""
echo -e "${BOLD}${GREEN}======================================================${RESET}"
echo -e "${BOLD}${GREEN}   ローリングアップデート正常完了！${RESET}"
echo -e "${BOLD}${GREEN}   現アクティブスロット: ${TARGET_CONTAINER}${RESET}"
echo -e "${BOLD}${GREEN}======================================================${RESET}"
