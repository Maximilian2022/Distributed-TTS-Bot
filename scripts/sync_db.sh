#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# TTSBot SQLite / データ双方向安全同期スクリプト (WALモード整合性保証)
# ==============================================================================
# 使い方:
#   ./scripts/sync_db.sh push  # MasterUbuntu -> MasterDebian (通常運用時の定期同期)
#   ./scripts/sync_db.sh pull  # MasterDebian -> MasterUbuntu (Ubuntu復帰Preempt時の逆同期)
# ==============================================================================

MODE="${1:-push}"
DEBIAN_IP="192.168.11.248"
LOCAL_DATA="/var/lib/docker/volumes/bot_data/_data"
REMOTE_DATA="/var/lib/docker/volumes/bot_data/_data"
TEMP_BACKUP="/tmp/bot_sync_atomic.db"

# 整合性のあるSQLiteオンラインバックアップ関数
create_atomic_backup() {
    local src_db="$1"
    local out_db="$2"
    python3 -c "
import sqlite3, sys
try:
    src = sqlite3.connect('file:${src_db}?mode=ro', uri=True)
    dst = sqlite3.connect('${out_db}')
    src.backup(dst)
    dst.close()
    src.close()
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
"
}

if [ "$MODE" = "push" ]; then
    echo "[INFO] Starting DB Sync: MasterUbuntu -> MasterDebian..."

    # 安全ガード 1: 自機 (Ubuntu) が ACTIVE かどうか検証 (Standby/停止中のプッシュ禁止)
    is_local_active=0
    if docker exec ttsbot-blue test -f /tmp/ttsbot_active 2>/dev/null || docker exec ttsbot-green test -f /tmp/ttsbot_active 2>/dev/null; then
        is_local_active=1
    fi
    if [ "$is_local_active" -ne 1 ]; then
        echo "[SKIP] Local host (MasterUbuntu) is not ACTIVE. Skipping DB push."
        exit 0
    fi

    # 安全ガード 2: リモート (Debian) が現在 ACTIVE かどうか検証 (フェイルオーバー本番中の上書き絶対禁止)
    if ssh -o ConnectTimeout=3 "root@${DEBIAN_IP}" "docker exec ttsbot-blue test -f /tmp/ttsbot_active 2>/dev/null || docker exec ttsbot-green test -f /tmp/ttsbot_active 2>/dev/null"; then
        echo "[ABORT] Remote host (MasterDebian) is currently ACTIVE! Push aborted to prevent data overwrite."
        exit 0
    fi

    if [ ! -f "$LOCAL_DATA/bot.db" ]; then
        echo "[ERROR] Local DB not found: $LOCAL_DATA/bot.db"
        exit 1
    fi
    
    # 1. ローカルの完全整合性スナップショット生成
    create_atomic_backup "$LOCAL_DATA/bot.db" "$TEMP_BACKUP"
    
    # 2. リモートへ bot.db をアトミック転送
    rsync -az "$TEMP_BACKUP" "root@${DEBIAN_IP}:${REMOTE_DATA}/bot.db"
    rm -f "$TEMP_BACKUP"
    
    # 3. リモート側の古い WAL/SHM をクリーンアップ (単一整合性DBとしてロードさせるため)
    ssh root@${DEBIAN_IP} "rm -f ${REMOTE_DATA}/bot.db-wal ${REMOTE_DATA}/bot.db-shm"
    
    # 4. アバター画像およびキャッシュjsonの増分同期
    if [ -d "$LOCAL_DATA/avatars" ]; then
        rsync -az --delete "$LOCAL_DATA/avatars/" "root@${DEBIAN_IP}:${REMOTE_DATA}/avatars/"
    fi
    for f in voices_cache.json mycoeiroink_cache.json; do
        if [ -f "$LOCAL_DATA/$f" ]; then
            rsync -az "$LOCAL_DATA/$f" "root@${DEBIAN_IP}:${REMOTE_DATA}/$f"
        fi
    done
    echo "[OK] DB Sync to Debian completed successfully."

elif [ "$MODE" = "pull" ]; then
    echo "[INFO] Starting Reverse DB Sync: MasterDebian -> MasterUbuntu (Preempt Recovery)..."
    # リモート(Debian)側で整合性バックアップを生成
    ssh root@${DEBIAN_IP} "python3 -c '
import sqlite3
src = sqlite3.connect(\"file:${REMOTE_DATA}/bot.db?mode=ro\", uri=True)
dst = sqlite3.connect(\"/tmp/bot_sync_atomic_pull.db\")
src.backup(dst)
dst.close()
src.close()
'"
    # ローカルへ転送
    rsync -az "root@${DEBIAN_IP}:/tmp/bot_sync_atomic_pull.db" "$LOCAL_DATA/bot.db"
    ssh root@${DEBIAN_IP} "rm -f /tmp/bot_sync_atomic_pull.db"
    
    # ローカルの古い WAL/SHM をクリーンアップ
    rm -f "$LOCAL_DATA/bot.db-wal" "$LOCAL_DATA/bot.db-shm"
    
    # アバター画像およびキャッシュjsonをDebianから逆同期
    if ssh root@${DEBIAN_IP} "[ -d ${REMOTE_DATA}/avatars ]"; then
        rsync -az --delete "root@${DEBIAN_IP}:${REMOTE_DATA}/avatars/" "$LOCAL_DATA/avatars/"
    fi
    for f in voices_cache.json mycoeiroink_cache.json; do
        rsync -az "root@${DEBIAN_IP}:${REMOTE_DATA}/$f" "$LOCAL_DATA/$f" || true
    done
    echo "[OK] Reverse DB Sync from Debian completed successfully."

else
    echo "Usage: $0 [push|pull]"
    exit 1
fi
