#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# MasterDebian を Warm-Standby 待機モードへ安全降格するスクリプト (Ubuntu復帰時用)
# ==============================================================================

echo "[$(date '+%Y-%m-%d %H:%M:%S')] [HA-Demote] Demoting MasterDebian to Warm-Standby..."

cd /root/ttsbot

# 0. Preempt クールダウンタイムスタンプ記録 (Watchdogの誤昇格を60秒間抑止)
date +%s > /tmp/ttsbot_preempt_cooldown

get_current_slot() {
    if docker ps -a --format '{{.Names}}' | grep -q '^ttsbot-green$'; then
        if docker ps --format '{{.Names}}' | grep -q '^ttsbot-green$'; then
            echo "green"
            return
        fi
    fi
    echo "blue"
}

TARGET_SLOT=$(get_current_slot)
TARGET_CNAME="ttsbot-${TARGET_SLOT}"

# 1. Cloudflare Tunnel 停止
docker stop ttsbot-tunnel 2>/dev/null || true

# 2. コンテナを安全停止 (blue/green 両方停止して単一スロットStandby化)
docker stop -t 5 ttsbot-blue ttsbot-green 2>/dev/null || true

# 3. active_slot.txt をホスト側から確実に standby にリセット (AUTO RECOVERY誤発動防止)
mkdir -p /var/lib/docker/volumes/bot_data/_data
echo "standby" > /var/lib/docker/volumes/bot_data/_data/active_slot.txt

# 4. スタンバイモードで再起動 (ウォームアップ完了で待機)
TTSBOT_STANDBY=1 docker compose up -d "$TARGET_CNAME"

# 起動待ち (最大15秒待機してウォームアップ確認 & 残留フラグ消去)
for i in {1..15}; do
    if docker exec "$TARGET_CNAME" test -f /tmp/ttsbot_ready 2>/dev/null; then
        break
    fi
    sleep 1
done

docker exec "$TARGET_CNAME" rm -f /tmp/ttsbot_active /tmp/ttsbot_activate 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] [HA-Demote] MasterDebian (${TARGET_CNAME}) is now in Warm-Standby mode (memory warm, Gateway disconnected)."
