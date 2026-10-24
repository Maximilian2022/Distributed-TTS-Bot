#!/usr/bin/env bash
# ==============================================================================
# TTSBot Worker Nodes Status & Logs Diagnostic Tool
# AI Assistant & Administrator CLI Inspector
# ==============================================================================

set -e

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
RESET="\033[0m"

echo -e "${BOLD}${CYAN}======================================================${RESET}"
echo -e "${BOLD}${CYAN}  TTSBot Compute Worker 診断 & ログビューア${RESET}"
echo -e "${BOLD}${CYAN}======================================================${RESET}"

# 検査対象ノード一覧 (引数または環境変数 TTSBOT_WORKER_NODES から指定可能: IP:PORT:LABEL)
NODES=()
if [ $# -gt 0 ]; then
  NODES=("$@")
elif [ -n "${TTSBOT_WORKER_NODES:-}" ]; then
  IFS="," read -ra NODES <<< "$TTSBOT_WORKER_NODES"
else
  # デフォルトのサンプルノード
  NODES=(
    "127.0.0.1:50030:Local Worker (Docker)"
  )
fi

for item in "${NODES[@]}"; do
  IFS=":" read -r IP PORT LABEL <<< "$item"
  echo ""
  echo -e "${BOLD}▶ [Node] ${LABEL} - http://${IP}:${PORT}${RESET}"

  # 1. 接続性テスト (ping)
  if ping -c 1 -W 1 "$IP" > /dev/null 2>&1; then
    echo -e "  ネットワーク疎通: ${GREEN}OK (Ping応答あり)${RESET}"
  else
    echo -e "  ネットワーク疎通: ${YELLOW}PING無応答 (OS側でICMP無効またはオフライン)${RESET}"
  fi

  # 2. /logs エンドポイントの確認 (v3.4.6〜)
  LOGS_JSON=$(curl -s --connect-timeout 2 --max-time 4 "http://${IP}:${PORT}/logs?lines=15" 2>/dev/null || true)
  if [ -n "$LOGS_JSON" ] && echo "$LOGS_JSON" | grep -q '"agent_version"'; then
    AGENT_VER=$(echo "$LOGS_JSON" | grep -o '"agent_version": "[^"]*"' | cut -d'"' -f4 || echo "unknown")
    PY_VER=$(echo "$LOGS_JSON" | grep -o '"python_version": "[^"]*"' | cut -d'"' -f4 || echo "unknown")
    NODE_ID=$(echo "$LOGS_JSON" | grep -o '"node_id": "[^"]*"' | cut -d'"' -f4 || echo "unknown")
    
    if echo "$PY_VER" | grep -q "3.14"; then
      PY_COLOR="${GREEN}"
    else
      PY_COLOR="${YELLOW}"
    fi

    echo -e "  Agent状態: ${GREEN}稼働中 (API応答あり)${RESET}"
    echo -e "  Node ID:   ${NODE_ID}"
    echo -e "  Agent版:   v${AGENT_VER}"
    echo -e "  Python版:  ${PY_COLOR}${PY_VER}${RESET}"
    echo -e "  ${BOLD}直近のWorkerログ:${RESET}"
    
    # python3 で JSON 内の logs 配列を綺麗に表示
    echo "$LOGS_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    logs = data.get('logs', [])
    for l in logs[-10:]:
        print('    | ' + l)
except Exception as e:
    print('    | (ログ解析エラー: ' + str(e) + ')')
" || echo "    | (ログ表示不可)"

  else
    # 3. /status エンドポイントへのフォールバック (旧バージョン)
    STATUS_JSON=$(curl -s --connect-timeout 2 --max-time 4 "http://${IP}:${PORT}/status" 2>/dev/null || true)
    if [ -n "$STATUS_JSON" ] && echo "$STATUS_JSON" | grep -q '"agent_version"'; then
      AGENT_VER=$(echo "$STATUS_JSON" | grep -o '"agent_version": "[^"]*"' | cut -d'"' -f4 || echo "legacy")
      NODE_ID=$(echo "$STATUS_JSON" | grep -o '"node_id": "[^"]*"' | cut -d'"' -f4 || echo "legacy")
      echo -e "  Agent状態: ${YELLOW}稼働中 (旧バージョン /logs未対応)${RESET}"
      echo -e "  Node ID:   ${NODE_ID}"
      echo -e "  Agent版:   v${AGENT_VER} ${YELLOW}(要アップデート)${RESET}"
      echo -e "  Python版:  ${YELLOW}Python 3.11 (旧エージェント)${RESET}"
    else
      echo -e "  Agent状態: ${RED}応答なし (ポート ${PORT} に接続できません)${RESET}"
    fi
  fi
done

echo ""
echo -e "${BOLD}${CYAN}======================================================${RESET}"
echo -e "${BOLD}診断完了${RESET}"
