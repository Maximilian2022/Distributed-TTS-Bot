#!/usr/bin/env bash
# ==============================================================================
# MasterUbuntu OS起動時 / 復帰時 Preempt 調停スクリプト
# ==============================================================================
# 動作シーケンス:
# 1. Debian (192.168.11.248:8088/health) が稼働中かつ Active か確認
# 2. もし Debian が Active の場合:
#    a. 【データ先祖返り防止】Debian から最新の bot.db とアバターを逆同期 (pull)
#    b. 【スプリットブレイン防止】Debian を安全に Standby 待機へ降格
# 3. MasterUbuntu のコンテナ (router, ttsbot-blue, tunnel) を Active 起動
# ==============================================================================

set -euo pipefail

DEBIAN_IP="${TTSBOT_STANDBY_IP:-192.168.11.248}"
DEBIAN_HEALTH_URL="http://${DEBIAN_IP}:8088/health"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [PreemptGuard] $1"
}

cd "$PROJECT_ROOT"

log "Starting Preempt Guard on MasterUbuntu..."

# 1. Debian の稼働確認
DEBIAN_IS_ACTIVE=0
if curl -s --connect-timeout 2 --max-time 3 "$DEBIAN_HEALTH_URL" | grep -q '"status"'; then
    # Debian が応答している場合、Active かどうか確認 (blue/green 両対応)
    if ssh -o ConnectTimeout=3 "root@${DEBIAN_IP}" "docker exec ttsbot-blue test -f /tmp/ttsbot_active 2>/dev/null || docker exec ttsbot-green test -f /tmp/ttsbot_active 2>/dev/null"; then
        DEBIAN_IS_ACTIVE=1
    fi
fi

if [ "$DEBIAN_IS_ACTIVE" -eq 1 ]; then
    log "⚡ MasterDebian が現在本番稼働 (ACTIVE) 中であることを検知しました！"
    log "📥 [Step 1/3] データ先祖返り防止: Debian から最新の DB とアバターを逆同期します..."
    "$PROJECT_ROOT/scripts/sync_db.sh" pull
    
    log "🛑 [Step 2/3] スプリットブレイン防止: Debian を Warm-Standby 待機へ降格させます..."
    ssh "root@${DEBIAN_IP}" "/root/ttsbot/scripts/ha_demote_to_standby.sh"
    log "✅ Debian の降格完了。"
else
    log "ℹ️ MasterDebian は Active 稼働していません (通常起動シーケンスへ進みます)。"
fi

# 2. MasterUbuntu を Active 起動
log "🚀 [Step 3/3] MasterUbuntu を ACTIVE 起動します..."
ACTIVE_SLOT_FILE="/var/lib/docker/volumes/bot_data/_data/active_slot.txt"
TARGET_SLOT="green"

# Nginx ルーターの設定（default.conf）から現在向いているスロットを最優先検出
NGINX_CONF="$PROJECT_ROOT/docker/nginx/default.conf"
if [ -f "$NGINX_CONF" ]; then
    if grep -q "ttsbot-green:8080" "$NGINX_CONF"; then
        TARGET_SLOT="green"
    elif grep -q "ttsbot-blue:8080" "$NGINX_CONF"; then
        TARGET_SLOT="blue"
    fi
elif [ -f "$ACTIVE_SLOT_FILE" ]; then
    SAVED_SLOT=$(cat "$ACTIVE_SLOT_FILE" 2>/dev/null | tr -d ' \r\n' | sed 's/ubuntu://')
    if [ "$SAVED_SLOT" = "green" ] || [ "$SAVED_SLOT" = "blue" ]; then
        TARGET_SLOT="$SAVED_SLOT"
    fi
fi

TARGET_CONTAINER="ttsbot-${TARGET_SLOT}"
OTHER_SLOT="blue"
if [ "$TARGET_SLOT" = "blue" ]; then
    OTHER_SLOT="green"
fi
OTHER_CONTAINER="ttsbot-${OTHER_SLOT}"

log "🎯 起動対象アクティブスロット: ${TARGET_CONTAINER} (旧スロット ${OTHER_CONTAINER} は停止維持)"

mkdir -p /var/lib/docker/volumes/bot_data/_data
echo "ubuntu:${TARGET_SLOT}" > "$ACTIVE_SLOT_FILE"

# スプリットブレイン・二重接続防止: 旧スロットコンテナを明示的に停止
docker stop -t 5 "$OTHER_CONTAINER" 2>/dev/null || true

docker compose up -d router "$TARGET_CONTAINER" tunnel

# コンテナがウォームアップ完了したらアクティベート (最大30秒待機)
for i in {1..30}; do
    if docker exec "$TARGET_CONTAINER" test -f /tmp/ttsbot_ready 2>/dev/null || docker exec "$TARGET_CONTAINER" test -f /tmp/ttsbot_active 2>/dev/null; then
        break
    fi
    sleep 1
done

docker exec "$TARGET_CONTAINER" touch /tmp/ttsbot_activate 2>/dev/null || docker kill -s USR1 "$TARGET_CONTAINER" 2>/dev/null || true

# 自機のヘルスチェック 200 OK 応答を確認 (最大20秒)
log "🔍 MasterUbuntu の健全稼働 (/health 200 OK) を確認中..."
for i in {1..20}; do
    if curl -s --connect-timeout 2 --max-time 3 "http://127.0.0.1:8088/health" | grep -q '"status"'; then
        log "✅ MasterUbuntu は正常稼働を確認しました。"
        break
    fi
    sleep 1
done

log "✨ MasterUbuntu の Preempt (主権奪還) が完了しました！"
