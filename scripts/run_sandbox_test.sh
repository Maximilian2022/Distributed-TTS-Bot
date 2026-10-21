#!/usr/bin/env bash
# ==============================================================================
# TTSBot Ephemeral Sandbox Runner (Docker Sandbox Test Helper)
#
# 【目的】
# 本番運用環境（ttsbot-blue/green, 他本番コンテナ群）に一切影響を与えず、
# 一時的な隔離コンテナを起動してテスト・検証を実行し、終了後に確実に自動破棄する。
#
# 【安全設計】
# 1. コンテナ名は一意（ttsbot-sandbox-XXXXX）とし、本番コンテナ名と絶対に衝突させない
# 2. ホストポートはバインドせず、ネットワーク競合を防止
# 3. 本番の Named Volume (bot_data 等) はマウントせず、一時ディレクトリ (tmpfs/mktemp) を使用
# 4. コードディレクトリ等は読み取り専用 (:ro) でマウントし、意図しない改変を防止
# 5. trap EXIT INT TERM により、エラーや中断時でも必ずコンテナと一時ファイルを破棄
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
RESET="\033[0m"

SANDBOX_ID="ttsbot-sandbox-$(date +%s)-$RANDOM"
IMAGE="${IMAGE:-ttsbot-ttsbot-blue:latest}"
TEMP_DATA_DIR="$(mktemp -d -t ttsbot-sandbox-data-XXXXXX)"

cleanup() {
  local exit_code=$?
  echo ""
  echo -e "${YELLOW}🧹 [Sandbox] クリーンアップ実行中... (${SANDBOX_ID})${RESET}"
  
  # コンテナの強制停止・破棄
  if docker ps -a --format '{{.Names}}' | grep -q "^${SANDBOX_ID}$"; then
    docker rm -f "${SANDBOX_ID}" >/dev/null 2>&1 || true
    echo -e "  ✓ 一時コンテナ破棄完了"
  fi

  # 一時ディレクトリの削除
  if [[ -d "${TEMP_DATA_DIR}" ]]; then
    rm -rf "${TEMP_DATA_DIR}" >/dev/null 2>&1 || true
    echo -e "  ✓ 一時データディレクトリ削除完了"
  fi

  echo -e "${GREEN}✓ [Sandbox] クリーンアップ完了 (ゼロフットプリント維持)${RESET}"
  exit ${exit_code}
}

trap cleanup EXIT INT TERM

echo -e "${BOLD}${CYAN}======================================================${RESET}"
echo -e "${BOLD}${CYAN}  TTSBot 安全サンドボックス テスト実行ツール${RESET}"
echo -e "${BOLD}${CYAN}======================================================${RESET}"
echo -e "  実行ID       : ${BOLD}${SANDBOX_ID}${RESET}"
echo -e "  対象イメージ : ${BOLD}${IMAGE}${RESET}"
echo -e "  一時データ   : ${TEMP_DATA_DIR}"
echo ""

# 実行コマンドの判定 (引数があればそれを実行、なければ基本健全性テスト)
if [[ $# -gt 0 ]]; then
  TEST_CMD=("$@")
else
  # デフォルト: Python構文・モジュールインポート・OpenJTalk初期化検証
  TEST_CMD=(python -c '
import sys
print(f"[Sandbox] Python バージョン: {sys.version}")
import discord
print(f"[Sandbox] discord.py インポート成功: {discord.__version__}")
import aiohttp
print(f"[Sandbox] aiohttp インポート成功: {aiohttp.__version__}")
import src.version
print(f"[Sandbox] TTSBot バージョン: {src.version.BUILD_VERSION}")
import src.tts.manager
print("[Sandbox] TTS Manager モジュールロード成功")
print("[Sandbox] 基本健全性テスト 全て合格！")
')
fi

echo -e "${BOLD}▶ サンドボックスコンテナ起動中...${RESET}"

docker run \
  --name "${SANDBOX_ID}" \
  --rm \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONPATH=/app \
  -e TZ=Asia/Tokyo \
  -e TTSBOT_STANDBY=1 \
  -v "${PROJECT_ROOT}/src:/app/src:ro" \
  -v "${PROJECT_ROOT}/scratch:/app/scratch:ro" \
  -v "${PROJECT_ROOT}/config.yaml:/app/config.yaml:ro" \
  -v "${PROJECT_ROOT}/openjtalk_voices:/app/openjtalk_voices:ro" \
  -v "${TEMP_DATA_DIR}:/app/data" \
  -w /app \
  "${IMAGE}" \
  "${TEST_CMD[@]}"

echo ""
echo -e "${BOLD}${GREEN}🎉 [Sandbox] テストが正常に完了しました！${RESET}"
