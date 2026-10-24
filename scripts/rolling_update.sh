#!/usr/bin/env bash
# ==============================================================================
# TTSBot 最小瞬断・ローリングアップデートスクリプト
# ==============================================================================
# 使い方:
#   ./scripts/rolling_update.sh             # 通常の高速リロード (マウントコード変更反映)
#   ./scripts/rolling_update.sh --build     # 新イメージを裏でビルドしてから切り替え
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
echo -e "${BOLD}${CYAN}   TTSBot ローリングアップデート開始${RESET}"
echo -e "${BOLD}${CYAN}======================================================${RESET}"

# 1. 現在の稼働状態チェック
CURRENT_APP_CONTAINER=$(docker compose ps -q app 2>/dev/null || true)
if [ -n "$CURRENT_APP_CONTAINER" ]; then
  CURRENT_STATUS=$(docker inspect --format '{{.State.Status}}' "$CURRENT_APP_CONTAINER" 2>/dev/null || echo "unknown")
  log_info "現在稼働中の本体コンテナ: ${BOLD}${CURRENT_APP_CONTAINER:0:12}${RESET} (Status: ${CURRENT_STATUS})"
else
  log_warn "稼働中の app コンテナが検出されませんでした。新規起動を行います。"
fi

# 2. ビルドフェーズ (オプション)
# 稼働中のコンテナに一切触れず、裏で完全ビルドを完了させることでダウンタイムを最小化
if [ "$DO_BUILD" -eq 1 ]; then
  log_info "裏で新イメージのビルドを開始します (※稼働中のBot通話・WebUIは一切影響を受けません)..."
  BUILD_CMD=(docker compose build)
  if [ "$NO_CACHE" -eq 1 ]; then
    BUILD_CMD+=(--no-cache)
  fi
  BUILD_CMD+=(app)
  
  "${BUILD_CMD[@]}"
  log_ok "イメージビルド完了！"
fi

# 3. 最小瞬断リロード (所要時間 約0.8〜1.5秒)
log_info "⚡ コンテナの安全切り替え・リロードを実行中..."
docker compose up -d --no-deps app

# 4. ヘルスチェック・復帰確認 (最大30秒)
log_info "新コンテナのヘルスチェック (/health 200 OK) を待機中..."
HEALTHY=0
for i in {1..30}; do
  WEB_PORT=$(docker compose exec -T app python -c 'import os; print(os.getenv("WEB_PORT", "8080"))' 2>/dev/null || echo "8080")
  WEB_PORT=$(echo "$WEB_PORT" | tr -d '\r\n')
  
  if docker compose exec -T app python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${WEB_PORT}/health', timeout=2).status == 200 else 1)" 2>/dev/null; then
    HEALTHY=1
    break
  fi
  sleep 1
done

if [ "$HEALTHY" -eq 1 ]; then
  log_ok "✨ 新コンテナの健全復帰 (/health 200 OK) を確認しました！"
else
  log_err "⚠️ ヘルスチェックがタイムアウトしました。ログを確認してください:"
  docker compose logs --tail 30 app || true
  exit 1
fi

# 5. 不要イメージのゴミ掃除 (オプション)
if [ "$DO_CLEAN" -eq 1 ]; then
  log_info "不要なダングリングイメージ・古いキャッシュを掃除中..."
  docker image prune -f || true
  log_ok "クリーンアップ完了"
fi

echo ""
echo -e "${BOLD}${GREEN}======================================================${RESET}"
echo -e "${BOLD}${GREEN}   🎉 ローリングアップデート正常完了！${RESET}"
echo -e "${BOLD}${GREEN}======================================================${RESET}"
