#!/usr/bin/env bash
# ==============================================================================
# TTSBot HA Watchdog on MasterDebian
# MasterUbuntu (192.168.11.245) の死活監視と自動フェイルオーバー
# ==============================================================================

UBUNTU_HEALTH_URL="http://192.168.11.245:8088/health"
FAIL_COUNT=0
FAIL_THRESHOLD=6  # 6回連続失敗 (約30秒) でフェイルオーバー (一時スパイクによる誤昇格防止)
CHECK_INTERVAL=5
COOLDOWN_FILE="/tmp/ttsbot_preempt_cooldown"
COOLDOWN_SECONDS=60  # Preempt降格後の自動昇格抑止秒数

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [HA-Watchdog] $1"
}

get_running_slot() {
    if docker ps --format '{{.Names}}' | grep -q '^ttsbot-green$'; then
        echo "green"
    elif docker ps --format '{{.Names}}' | grep -q '^ttsbot-blue$'; then
        echo "blue"
    else
        echo "blue"
    fi
}

is_ubuntu_healthy() {
    curl -s --connect-timeout 3 --max-time 4 "$UBUNTU_HEALTH_URL" | grep -q '"status"' || return 1
    return 0
}

is_debian_active() {
    local slot
    slot=$(get_running_slot)
    docker exec "ttsbot-${slot}" test -f /tmp/ttsbot_active 2>/dev/null && return 0
    return 1
}

is_in_preempt_cooldown() {
    if [ -f "$COOLDOWN_FILE" ]; then
        local cooldown_ts
        cooldown_ts=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
        local now
        now=$(date +%s)
        local elapsed=$((now - cooldown_ts))
        if [ "$elapsed" -ge 0 ] && [ "$elapsed" -lt "$COOLDOWN_SECONDS" ]; then
            return 0
        fi
    fi
    return 1
}

promote_debian_to_active() {
    if is_in_preempt_cooldown; then
        log "⏸️ MasterUbuntu による Preempt 降格直後 (${COOLDOWN_SECONDS}秒クールダウン中) のため自動昇格を保留します。"
        return
    fi

    local slot
    slot=$(get_running_slot)
    local cname="ttsbot-${slot}"

    log "🚨 MasterUbuntu の無応答を検知！ MasterDebian (${cname}) を ACTIVE に昇格します..."
    
    cd /root/ttsbot
    
    # 1. Cloudflare Tunnel 起動 (WebUI へのトラフィック引き受け)
    docker compose up -d tunnel
    
    # 2. active_slot.txt を debian:${slot} に設定
    docker exec "$cname" bash -c "echo 'debian:${slot}' > /app/data/active_slot.txt" 2>/dev/null || true
    
    # 3. アクティベートシグナル送信 (Gateway接続 & VC復帰)
    docker exec "$cname" touch /tmp/ttsbot_activate 2>/dev/null || docker kill -s USR1 "$cname" 2>/dev/null || true
    
    log "✨ MasterDebian のアクティベート完了！ (Gateway接続 & VC自動復帰)"
}

log "HA Watchdog started. Monitoring MasterUbuntu ($UBUNTU_HEALTH_URL)..."

while true; do
    if is_ubuntu_healthy; then
        FAIL_COUNT=0
        # Ubuntu が正常復帰した場合はクールダウンファイルを掃除
        rm -f "$COOLDOWN_FILE" 2>/dev/null || true

        # MasterUbuntu が健全稼働中にもかかわらず Debian が ACTIVE の場合は、自動降格 (Auto Demote / Preempt)
        # これによりスプリットブレイン、トンネル競合、Discord 4006 二重接続を自動的に完全防止する
        if is_debian_active; then
            log "✨ MasterUbuntu の健全稼働 (/health 200 OK) を検知！ MasterDebian を Standby 待機へ自動降格します (Auto-Demote)..."
            /root/ttsbot/scripts/ha_demote_to_standby.sh
        fi
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        log "⚠️ MasterUbuntu ヘルスチェック失敗 ($FAIL_COUNT/$FAIL_THRESHOLD)"
        
        if [ "$FAIL_COUNT" -ge "$FAIL_THRESHOLD" ]; then
            if ! is_debian_active; then
                promote_debian_to_active
            fi
        fi
    fi
    sleep "$CHECK_INTERVAL"
done
