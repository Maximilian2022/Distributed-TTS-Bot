#!/usr/bin/env bash
# ==============================================================================
# [DEPRECATED] TTSBot 旧 HA Watchdog スクリプト
# ==============================================================================
# 現在の Distributed-TTS-Bot では、Python 製 HA オーケストレータ
# (src/ha_watchdog.py) および Docker Compose (standby-watchdog) に完全移行しました。
#
# 【推奨される起動方法】:
#   docker compose -f docker-compose.slave.yml up -d
#   または
#   python3 -m src.ha_watchdog
# ==============================================================================

echo "======================================================================"
echo "⚠️  [DEPRECATED] このシェルスクリプトは旧レガシー環境用です。"
echo "現在、HA Watchdog は Python 自律デーモン (src.ha_watchdog) に移行しました。"
echo ""
echo "Standby Watchdog の起動は以下のコマンドを実行してください:"
echo "  docker compose -f docker-compose.slave.yml up -d"
echo "  または直接実行: python3 -m src.ha_watchdog"
echo "======================================================================"
exit 0
