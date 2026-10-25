#!/usr/bin/env bash
# ==============================================================================
# Distributed TTS Bot - 安全アップデートスクリプト (Safe Zero-Downtime Update)
# ==============================================================================
# 特徴:
#   1. 事前イメージ取得 (docker compose pull) によりコンテナ停止時間をわずか約2秒に短縮
#   2. HA環境の相手側Standby Slaveが誤昇格(Split-Brain)しないよう更新中シグナルを自動通知
#   3. SQLiteデータベースおよび重要設定ファイル (data/) を自動バックアップ
#   4. 更新完了後、接続中のボランティアワーカーへ最新コードを自動更新配信
#
# 使用方法:
#   ./update.sh                 # Docker Hub 最新イメージによる安全アップデート (推奨・最速)
#   ./update.sh --build         # ローカルで Dockerfile からビルドしてアップデート
#   ./update.sh --no-cache      # キャッシュ完全破棄リビルドしてアップデート
#   ./update.sh --clean         # アップデート後に古い不要イメージを自動削除
# ==============================================================================

set -euo pipefail

# カラー設定
BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
MAGENTA="\033[35m"
RESET="\033[0m"

log_info() { echo -e "${CYAN}[INFO]${RESET} $1"; }
log_ok()   { echo -e "${GREEN}[OK]${RESET} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${RESET} $1"; }
log_err()  { echo -e "${RED}[ERROR]${RESET} $1"; }
log_step() { echo -e "\n${BOLD}${MAGENTA}==>${RESET} ${BOLD}$1${RESET}"; }

# プロジェクトディレクトリの特定
if [ -f "docker-compose.yml" ]; then
  PROJECT_ROOT="$(pwd)"
elif [ -f "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/docker-compose.yml" ]; then
  PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
elif [ -d "/root/ttsbot" ] && [ -f "/root/ttsbot/docker-compose.yml" ]; then
  PROJECT_ROOT="/root/ttsbot"
elif [ -d "$HOME/ttsbot" ] && [ -f "$HOME/ttsbot/docker-compose.yml" ]; then
  PROJECT_ROOT="$HOME/ttsbot"
else
  log_err "docker-compose.yml が見つかりません。TTSBot のプロジェクトディレクトリで実行してください。"
  exit 1
fi

cd "$PROJECT_ROOT"
log_info "対象プロジェクトディレクトリ: ${BOLD}${PROJECT_ROOT}${RESET}"

# コマンドオプションのパース
DO_BUILD=0
NO_CACHE=0
DO_CLEAN=0
SKIP_GIT=0

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
    --skip-git)
      SKIP_GIT=1
      ;;
    -h|--help)
      echo "使用方法: $0 [オプション]"
      echo ""
      echo "オプション:"
      echo "  --pull       Docker Hub 公開イメージを事前Pullして更新 (デフォルト)"
      echo "  --build      ローカルで Dockerfile からビルドして更新"
      echo "  --no-cache   ビルドキャッシュを使用せずに完全リビルド (--build と併用)"
      echo "  --clean      更新完了後に不要な旧Dockerイメージを自動削除"
      echo "  --skip-git   git pull によるソース取得をスキップ"
      echo "  -h, --help   このヘルプメッセージを表示"
      exit 0
      ;;
    --pull)
      DO_BUILD=0
      ;;
    *)
      log_warn "未知の引数: $arg (無視されます)"
      ;;
  esac
done

echo -e "${BOLD}${CYAN}================================================================${RESET}"
echo -e "${BOLD}${CYAN}   🚀 Distributed TTS Bot 安全アップデートプロセス開始${RESET}"
echo -e "${BOLD}${CYAN}================================================================${RESET}"

# HA更新中フラグの設定 & trap登録 (スクリプト終了時に確実に削除)
cleanup() {
  if [ -f "data/.ha_updating" ]; then
    rm -f "data/.ha_updating" 2>/dev/null || true
    log_info "HA更新中フラグ (data/.ha_updating) を解除しました。"
  fi
}
trap cleanup EXIT INT TERM

# 1. HA相手側ノードの誤昇格抑止シグナル作成
log_step "1. HA保護シグナルの設定"
mkdir -p data
touch data/.ha_updating
log_ok "HA誤昇格抑止フラグ (data/.ha_updating) を有効化しました (相手側Slaveはフェイルオーバーを猶予します)"

# 2. 重要データのバックアップ
log_step "2. データベースおよび重要設定の自動バックアップ"
BACKUP_DIR="data_backup/$(date +'%Y%m%d_%H%M%S')"
mkdir -p "$BACKUP_DIR"
if compgen -G "data/*.db" > /dev/null || compgen -G "data/*.json" > /dev/null; then
  cp -rp data/*.db data/*.json "$BACKUP_DIR/" 2>/dev/null || true
  log_ok "バックアップ完了: ${BOLD}${BACKUP_DIR}${RESET}"
else
  log_info "バックアップ対象の既存データベースはまだありません。"
fi

# 3. 最新コードの取得 (Git管理環境の場合)
if [ "$SKIP_GIT" -eq 0 ] && [ -d ".git" ]; then
  log_step "3. 最新リポジトリコードの取得 (git pull)"
  if git status --porcelain 2>/dev/null | grep -q '^[ MADRCU?!]'; then
    log_warn "作業ツリーに変更があります。ローカル変更を保持しつつ最新を取得します..."
    git stash -q || true
    git pull origin main || git pull || log_warn "git pull に失敗しました。現在のコードで続行します。"
    git stash pop -q 2>/dev/null || true
  else
    git pull origin main || git pull || log_warn "git pull に失敗しました。現在のコードで続行します。"
  fi
  log_ok "Gitコード取得完了"
fi

# 4. 新イメージの事前取得フェーズ (稼働中のコンテナは止めずに裏で実行！)
log_step "4. 新イメージの事前準備 (バックグラウンド取得)"
if [ "$DO_BUILD" -eq 1 ]; then
  log_info "ローカルビルドを実行します (※稼働中のBot通話・WebUIは影響を受けません)..."
  BUILD_CMD=(docker compose build)
  if [ "$NO_CACHE" -eq 1 ]; then
    BUILD_CMD+=(--no-cache)
  fi
  BUILD_CMD+=(app)
  "${BUILD_CMD[@]}"
  log_ok "Dockerイメージのビルド完了！"
else
  log_info "Docker Hub から最新イメージを事前ダウンロード中 (※コンテナは停止しません)..."
  docker compose pull app || docker compose pull
  log_ok "最新イメージの事前取得完了！"
fi

# 5. 最小瞬断ロールオーバー (コンテナ切り替え、所要時間 約2秒)
log_step "5. コンテナの安全ロールオーバー (最小瞬断切り替え)"
log_info "新イメージへのコンテナ再作成を実行します..."
docker compose up -d --no-deps app
log_ok "新コンテナ起動完了！"

# 6. ヘルスチェック・復帰確認 (最大30秒待機)
log_step "6. 稼働確認 & ヘルスチェック"
log_info "新コンテナのヘルスチェック (/health 200 OK) を待機中..."
HEALTHY=0
for i in {1..30}; do
  if docker compose exec -T app python -c "import urllib.request, os, sys; p = os.getenv('WEB_PORT', '8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=2).status == 200 else 1)" 2>/dev/null; then
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

# 7. ボランティアワーカー & HA Standby Slave への最新化ブロードキャスト
log_step "7. 接続中ボランティアワーカー & HA Standby Slave への自動更新通知"
docker compose exec -T app python -c "
import urllib.request, os
p = os.getenv('WEB_PORT', '8080')
try:
    req = urllib.request.Request(f'http://127.0.0.1:{p}/api/worker/broadcast_update', data=b'{}', headers={'Content-Type': 'application/json', 'User-Agent': 'InternalUpdate'})
    urllib.request.urlopen(req, timeout=3)
    print('  [Worker Update] 接続中の全ボランティアワーカーへ更新通知を送信しました。')
except Exception:
    pass

try:
    req_ha = urllib.request.Request(f'http://127.0.0.1:{p}/api/ha/broadcast_update', data=b'{}', headers={'Content-Type': 'application/json', 'User-Agent': 'InternalUpdate'})
    urllib.request.urlopen(req_ha, timeout=3)
    print('  [HA Update] 全 HA Standby Slave ノードへ即時自己アップデート通知を送信しました。')
except Exception:
    pass
" 2>/dev/null || true

# 8. クリーンアップ (オプション)
if [ "$DO_CLEAN" -eq 1 ]; then
  log_step "8. 不要な旧Dockerイメージの掃除"
  docker image prune -f || true
  log_ok "クリーンアップ完了"
fi

echo ""
echo -e "${BOLD}${GREEN}================================================================${RESET}"
echo -e "${BOLD}${GREEN}   🎉 Distributed TTS Bot のアップデートが正常に完了しました！${RESET}"
echo -e "${BOLD}${GREEN}================================================================${RESET}"
echo ""
