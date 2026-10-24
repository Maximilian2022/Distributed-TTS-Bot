#!/usr/bin/env bash
# ==============================================================================
# TTSBot Compute Worker (Debian/Linux) ワンライナー自動セットアップスクリプト v2
#
# 実行方法 (Debian側でワンライナー):
#   curl -fsSL http://<MASTER_HOST>:8080/setup-worker.sh | bash
#
# 機能:
#   - Docker / docker compose の自動インストール
#   - TTS エンジンコンテナ (VOICEVOX / SHAREVOX / COEIROINK) の自動展開
#   - Worker Agent コンテナ (自律管理・自己アップデート・ハートビート) の自動起動
#   - 共用マシン配慮: CPUコア数・RAMを自動検出してリソース制限を動的設定
# ==============================================================================

set -euo pipefail

# カラー定義
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_BLUE="\033[34m"
C_GREEN="\033[32m"
C_YELLOW="\033[33m"
C_RED="\033[31m"
C_CYAN="\033[36m"

MASTER_URL="${TTSBOT_MASTER_URL:-http://localhost:8088}"
MASTER_WS_URL="${TTSBOT_MASTER_WS_URL:-}"
WORKER_SECRET="${TTSBOT_WORKER_SECRET:-}"
WORK_DIR="${TTSBOT_WORK_DIR:-${HOME}/ttsbot-worker}"
AGENT_IMAGE="${TTSBOT_AGENT_IMAGE:-python:3.14-slim}"

# コマンドライン引数のパース (ワンライナー対応)
while [ $# -gt 0 ]; do
    case "$1" in
        --master)
            MASTER_URL="$2"
            shift 2
            ;;
        --master-ws)
            MASTER_WS_URL="$2"
            shift 2
            ;;
        --secret)
            WORKER_SECRET="$2"
            shift 2
            ;;
        --work-dir)
            WORK_DIR="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

# MASTER_WS_URL が渡された場合、未指定の MASTER_URL を自動補完
if [ -n "${MASTER_WS_URL}" ] && [ "${MASTER_URL}" = "http://localhost:8088" ]; then
    DERIVED_HTTP=$(echo "${MASTER_WS_URL}" | sed -E 's|^ws://|http://|' | sed -E 's|^wss://|https://|' | sed -E 's|/api/worker/ws.*||')
    if [ -n "${DERIVED_HTTP}" ]; then
        MASTER_URL="${DERIVED_HTTP}"
    fi
fi

echo -e "${C_BOLD}${C_CYAN}"
echo "=============================================================================="
echo "   🚀 TTSBot Compute Worker 自動セットアップ (Debian/Linux) v2              "
echo "   Master: ${MASTER_URL}"
if [ -n "${MASTER_WS_URL}" ]; then
echo "   Master WS: ${MASTER_WS_URL} (WAN/NAT越えモード)"
fi
echo "=============================================================================="
echo -e "${C_RESET}"
echo -e "${C_BLUE}ℹ️  作業ディレクトリ: ${WORK_DIR}${C_RESET}"

mkdir -p "${WORK_DIR}/docker/sharevox"
mkdir -p "${WORK_DIR}/docker/coeiroink"
mkdir -p "${WORK_DIR}/docker/agent"
mkdir -p "${WORK_DIR}/coeiroink_models"
cd "${WORK_DIR}"

# 1. 依存コマンドチェック
echo -e "\n${C_BLUE}🔍 [1/5] システム環境と依存コマンドをチェック中...${C_RESET}"

NEED_SUDO=""
if [ "$EUID" -ne 0 ]; then
    if command -v sudo > /dev/null 2>&1; then
        NEED_SUDO="sudo"
    else
        echo -e "${C_RED}❌ エラー: root 権限または sudo が必要です。${C_RESET}"
        exit 1
    fi
fi

# 基本パッケージ確認
MISSING_PKGS=""
for pkg in curl git rsync tar; do
    if ! command -v "$pkg" > /dev/null 2>&1; then
        MISSING_PKGS="${MISSING_PKGS} ${pkg}"
    fi
done

if [ -n "$MISSING_PKGS" ]; then
    echo -e "${C_YELLOW}📦 不足パッケージをインストール中:${MISSING_PKGS}${C_RESET}"
    $NEED_SUDO apt-get update -y
    $NEED_SUDO apt-get install -y $MISSING_PKGS
fi

# Docker チェック
if ! command -v docker > /dev/null 2>&1; then
    echo -e "${C_YELLOW}⚠️  Docker が見つかりません。公式スクリプトから自動インストールします...${C_RESET}"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    $NEED_SUDO sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    if [ -n "${USER:-}" ] && [ "$USER" != "root" ]; then
        $NEED_SUDO usermod -aG docker "$USER" || true
        echo -e "${C_YELLOW}⚠️  docker グループに追加しました。セッションを再起動するか 'newgrp docker' を実行してください。${C_RESET}"
    fi
    echo -e "${C_GREEN}✅ Docker のインストールが完了しました。${C_RESET}"
fi

# Docker Compose チェック (docker compose v2)
if ! docker compose version > /dev/null 2>&1; then
    echo -e "${C_YELLOW}⚠️  docker compose プラグインをインストール中...${C_RESET}"
    $NEED_SUDO apt-get update -y
    $NEED_SUDO apt-get install -y docker-compose-plugin
fi

echo -e "${C_GREEN}✅ Docker 環境チェック完了: $(docker --version) / $(docker compose version)${C_RESET}"

# カーネルのポート予約 (エフェメラルポートの衝突による起動ループ防止)
echo -e "${C_BLUE}🔒 ワーカーポート (50020-50040) を Linux カーネルの予約ポートに設定中...${C_RESET}"
if command -v sysctl > /dev/null 2>&1; then
    echo '# TTSBot Worker/Engine ports reservation (VOICEVOX:50021, SHAREVOX:50025, Agent:50030, COEIROINK:50031-50032)' | $NEED_SUDO tee /etc/sysctl.d/99-ttsbot-ports.conf > /dev/null
    echo 'net.ipv4.ip_local_reserved_ports = 50020-50040' | $NEED_SUDO tee -a /etc/sysctl.d/99-ttsbot-ports.conf > /dev/null
    $NEED_SUDO sysctl -p /etc/sysctl.d/99-ttsbot-ports.conf > /dev/null 2>&1 || true
    echo -e "${C_GREEN}✅ ポート予約設定完了 (net.ipv4.ip_local_reserved_ports = 50020-50040)${C_RESET}"
fi

# 2. ハードウェアスペック自動検出 → リソース制限を動的決定
echo -e "\n${C_BLUE}🖥️  [2/5] ハードウェアスペックを自動検出してリソース制限を決定中...${C_RESET}"

# CPU コア数 (共用マシン配慮: 最大半分まで使用)
TOTAL_CPUS=$(nproc 2>/dev/null || echo "2")
ALLOC_CPUS=$(python3 -c "import math; print(max(1, math.floor(${TOTAL_CPUS} / 2)))" 2>/dev/null || echo "2")

# RAM (MiB → GiB, 共用配慮: 最大半分まで)
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "4000000")
TOTAL_RAM_GB=$(python3 -c "print(round(${TOTAL_RAM_KB} / 1024 / 1024, 1))" 2>/dev/null || echo "4")
ALLOC_RAM_GB=$(python3 -c "print(max(1, round(${TOTAL_RAM_KB} / 1024 / 1024 / 2, 0)))" 2>/dev/null || echo "2")

# スレッド数 (共用配慮: 割り当てCPU数に準拠)
THREAD_NUM=$(python3 -c "print(max(1, int(${ALLOC_CPUS} / 1)))" 2>/dev/null || echo "2")

# エンジン1台あたりのRAM上限 (GB)
RAM_VV=$(python3 -c "print(max(1, int(${ALLOC_RAM_GB} * 0.35)))" 2>/dev/null || echo "2")
RAM_SV=$(python3 -c "print(max(1, int(${ALLOC_RAM_GB} * 0.30)))" 2>/dev/null || echo "2")
RAM_CI=$(python3 -c "print(max(1, int(${ALLOC_RAM_GB} * 0.35)))" 2>/dev/null || echo "2")

# 環境変数としても export しておく (docker compose 用安全策)
export ALLOC_CPUS ALLOC_RAM_GB THREAD_NUM RAM_VV RAM_SV RAM_CI

# GPUチェック
HAS_GPU="false"
GPU_NAME=""
GPU_VRAM_MB="0"
if command -v nvidia-smi > /dev/null 2>&1 && nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | grep -q .; then
    HAS_GPU="true"
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | tr -d '\r')
    GPU_VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d '\r' | awk '{print $1}')
    echo -e "${C_GREEN}  ✅ GPU 検出: ${GPU_NAME} (${GPU_VRAM_MB}MB VRAM)${C_RESET}"
    VOICEVOX_IMAGE="voicevox/voicevox_engine:nvidia-ubuntu20.04-latest"
    COEIROINK_DIR="COEIROINK_LINUX_GPU_v.2025.4.1"
    COEIROINK_ZIP_URL="https://www.dropbox.com/scl/fi/pisvh3pwlinz6hdep2fcr/COEIROINK_LINUX_GPU_v.2025.4.1.zip?rlkey=4dg1368zzdbplahcjdbqoj2tg&st=3k7yonjs&dl=1"
    COEIROINK_BASE_IMAGE="nvidia/cuda:11.8.0-base-ubuntu22.04"
    VV_DEPLOY=$(cat << EOF
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          memory: ${RAM_VV}G
EOF
)
    SV_DEPLOY=$(cat << EOF
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          memory: ${RAM_SV}G
EOF
)
    CI_DEPLOY=$(cat << EOF
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          memory: ${RAM_CI}G
EOF
)
else
    VOICEVOX_IMAGE="voicevox/voicevox_engine:cpu-ubuntu20.04-latest"
    echo -e "${C_BLUE}  ℹ️  CPU モードで実行します${C_RESET}"
    COEIROINK_DIR="COEIROINK_LINUX_CPU_v.2025.4.1"
    COEIROINK_ZIP_URL="https://www.dropbox.com/scl/fi/o7xnjfo0jw81otgn2jxar/COEIROINK_LINUX_CPU_v.2025.4.1.zip?rlkey=dt1z2bxrbo3ikm2iiioxh9wx7&st=tso7516f&dl=1"
    COEIROINK_BASE_IMAGE="ubuntu:22.04"
    VV_DEPLOY=$(cat << EOF
    deploy:
      resources:
        limits:
          cpus: "${ALLOC_CPUS}.0"
          memory: ${RAM_VV}G
EOF
)
    SV_DEPLOY=$(cat << EOF
    deploy:
      resources:
        limits:
          cpus: "${ALLOC_CPUS}.0"
          memory: ${RAM_SV}G
EOF
)
    CI_DEPLOY=$(cat << EOF
    deploy:
      resources:
        limits:
          cpus: "${ALLOC_CPUS}.0"
          memory: ${RAM_CI}G
EOF
)
fi

echo -e "  総CPU: ${TOTAL_CPUS}コア → 割り当て: ${ALLOC_CPUS}コア / ${THREAD_NUM}スレッド"
echo -e "  総RAM: ${TOTAL_RAM_GB}GB → 割り当て上限: ${ALLOC_RAM_GB}GB (エンジン計: VV=${RAM_VV}G, SV=${RAM_SV}G, CI=${RAM_CI}G)"
echo -e "  VOICEVOX イメージ: ${VOICEVOX_IMAGE}"

# LAN IP アドレス検出 (プライベートIP・デフォルトルート物理NIC優先)
LOCAL_IP=$(ip route get 1 2>/dev/null | awk '{print $7;exit}' || true)
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP=$(ip -4 addr show 2>/dev/null | grep -oP '(?<=inet\s)(192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2[0-9]|3[0-1])\.\d+\.\d+)' | head -n 1 || true)
fi
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
fi
echo -e "  LAN IP アドレス: ${C_GREEN}${LOCAL_IP}${C_RESET} (自動検出)"

# 3. 構成ファイルの展開
echo -e "\n${C_BLUE}📦 [3/5] コンテナ構成ファイルを展開中...${C_RESET}"

# ----- docker-compose.worker.yml (TTS エンジン群) -----
cat > "${WORK_DIR}/docker-compose.worker.yml" << COMPOSE_EOF
# ==============================================================================
# TTSBot Compute Worker (Debian 専用) - 自動生成ファイル
# CPU/RAM は起動時のハードウェアに合わせて自動設定済み
# ==============================================================================

name: ttsbot-worker-engines

services:
  worker-voicevox:
    image: ${VOICEVOX_IMAGE}
    container_name: ttsworker-voicevox
    restart: unless-stopped
    command: ["gosu", "user", "/opt/python/bin/python3", "./run.py", "--voicelib_dir", "/opt/voicevox_core/", "--runtime_dir", "/opt/onnxruntime/lib", "--host", "0.0.0.0", "--cpu_num_threads", "${THREAD_NUM}"]
    ports:
      - "50021:50021"
    environment:
      - OMP_NUM_THREADS=${THREAD_NUM}
      - MKL_NUM_THREADS=${THREAD_NUM}
${VV_DEPLOY}
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "2"

  worker-sharevox:
    build:
      context: ./docker/sharevox
      dockerfile: Dockerfile
    container_name: ttsworker-sharevox
    restart: unless-stopped
    ports:
      - "50025:50025"
    environment:
      - OMP_NUM_THREADS=${THREAD_NUM}
      - MKL_NUM_THREADS=${THREAD_NUM}
${SV_DEPLOY}
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "2"

  worker-coeiroink:
    build:
      context: ./docker/coeiroink
      dockerfile: Dockerfile
    container_name: ttsworker-coeiroink
    restart: unless-stopped
    ports:
      - "50031:50031"
    environment:
      - OMP_NUM_THREADS=${THREAD_NUM}
      - MKL_NUM_THREADS=${THREAD_NUM}
      - TORCH_NUM_THREADS=${THREAD_NUM}
      - OPENBLAS_NUM_THREADS=${THREAD_NUM}
    volumes:
      - ./coeiroink_models:/opt/coeiroink/${COEIROINK_DIR}/speaker_info
${CI_DEPLOY}
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "2"
COMPOSE_EOF

# ----- docker-compose.agent.yml (Worker Agent 常駐プロセス) -----
AGENT_GPU_SECTION=""
if [ "$HAS_GPU" = "true" ]; then
AGENT_GPU_SECTION=$(cat << 'EOF'
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          cpus: "0.5"
EOF
)
else
AGENT_GPU_SECTION=$(cat << 'EOF'
    deploy:
      resources:
        limits:
          cpus: "0.5"
EOF
)
fi

cat > "${WORK_DIR}/docker-compose.agent.yml" << AGENT_EOF
# ==============================================================================
# TTSBot Worker Agent - 常駐管理プロセス
# Master への自律登録・ハートビート・エンジン管理・自己アップデートを担当
# ==============================================================================

name: ttsbot-worker-agent

services:
  worker-agent:
    image: ${AGENT_IMAGE}
    container_name: ttsworker-agent
    restart: unless-stopped
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./:/work
      - ./docker/agent:/app
      - ./coeiroink_models:/coeiroink_models
      - ./docker-compose.worker.yml:/app/docker-compose.worker.yml
    working_dir: /app
    environment:
      - TZ=Asia/Tokyo
      - MASTER_URL=${MASTER_URL}
      - MASTER_WS_URL=${MASTER_WS_URL}
      - WORKER_SHARED_SECRET=${WORKER_SECRET}
      - WORKER_IP=${LOCAL_IP}
      - HAS_GPU=${HAS_GPU}
      - GPU_NAME=${GPU_NAME}
      - GPU_VRAM_MB=${GPU_VRAM_MB}
      - AGENT_PORT=50030
      - RSYNC_PORT=8730
      - IDLE_TIMEOUT_SECONDS=900
      - WORK_DIR=/work
      - WORKER_COMPOSE_FILE=/work/docker-compose.worker.yml
      - NODE_ID=worker_$(hostname)
    command: >
      sh -c "pip install --quiet --no-cache-dir --root-user-action=ignore aiohttp && python3 agent.py"
${AGENT_GPU_SECTION}
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
AGENT_EOF

# ----- Dockerfile類 -----

# SHAREVOX Dockerfile (GPUあり時は公式GPUイメージ+モデル注入、CPU時は軽量CPUバイナリ)
if [ "${HAS_GPU}" = "true" ]; then
cat > "${WORK_DIR}/docker/sharevox/Dockerfile" << 'EOF'
FROM ubuntu:20.04 AS downloader
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y curl tar ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/sv
RUN curl -sL "https://github.com/SHAREVOX/sharevox/releases/download/0.2.1/sharevox-linux-cpu-0.2.1.tar.gz" | tar -xzf - --strip-components=1

FROM sharevox/sharevox_engine:nvidia-ubuntu20.04-latest
USER root
RUN mkdir -p /opt/sharevox_engine/library_info
COPY --from=downloader /tmp/sv/model /opt/sharevox_engine/model
COPY --from=downloader /tmp/sv/speaker_info /opt/sharevox_engine/speaker_info
EXPOSE 50025
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gosu", "user", "/opt/python/bin/python3", "./run.py", "--use_gpu", "--voicelib_dir", "/opt/sharevox_core/", "--runtime_dir", "/opt/onnxruntime/lib", "--host", "0.0.0.0", "--port", "50025"]
EOF
else
cat > "${WORK_DIR}/docker/sharevox/Dockerfile" << EOF
FROM ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y curl tar ca-certificates libgomp1 libsndfile1 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/sharevox
RUN curl -sL "https://github.com/SHAREVOX/sharevox/releases/download/0.2.1/sharevox-linux-cpu-0.2.1.tar.gz" | tar -xzf - --strip-components=1 && chmod +x /opt/sharevox/run
EXPOSE 50025
ENTRYPOINT ["/opt/sharevox/run"]
CMD ["--host", "0.0.0.0", "--port", "50025", "--cpu_num_threads", "${THREAD_NUM}"]
EOF
fi

# COEIROINK Dockerfile & entrypoint
cat > "${WORK_DIR}/docker/coeiroink/Dockerfile" << EOF
FROM ${COEIROINK_BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y curl unzip ca-certificates libgomp1 libsndfile1 socat && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/coeiroink
RUN curl -L -o /tmp/coeiroink.zip "${COEIROINK_ZIP_URL}" && \\
    unzip -q /tmp/coeiroink.zip -d /opt/coeiroink && \\
    rm /tmp/coeiroink.zip && \\
    chmod +x /opt/coeiroink/${COEIROINK_DIR}/engine/engine
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 50031 50032
ENTRYPOINT ["/entrypoint.sh"]
EOF

cat > "${WORK_DIR}/docker/coeiroink/entrypoint.sh" << EOF
#!/bin/bash
set -e
DIR=\$(ls -d /opt/coeiroink/COEIROINK_LINUX_* | head -n 1)
cd "\$DIR/engine"
export LD_LIBRARY_PATH="\$DIR/engine:\$DIR/engine/torch/lib:/usr/local/cuda/lib64:\$LD_LIBRARY_PATH"
socat TCP-LISTEN:50031,fork,reuseaddr TCP:127.0.0.1:50032 &
exec "\$DIR/engine/engine"
EOF
chmod +x "${WORK_DIR}/docker/coeiroink/entrypoint.sh"

# agent.py を Master から取得 (なければ警告のみ)
echo -e "${C_BLUE}  📥 Worker Agent スクリプトを取得中...${C_RESET}"
if curl -fsSL "${MASTER_URL}/static/agent.py" -o "${WORK_DIR}/docker/agent/agent.py" 2>/dev/null; then
    echo -e "${C_GREEN}  ✅ agent.py を ${MASTER_URL} から取得しました。${C_RESET}"
else
    echo -e "${C_YELLOW}  ⚠️  agent.py の取得に失敗しました。${WORK_DIR}/docker/agent/agent.py を手動で配置してください。${C_RESET}"
fi

echo -e "${C_GREEN}✅ 構成ファイルの展開が完了しました。${C_RESET}"

# 3.5. COEIROINK モデルの同期
echo -e "\n${C_BLUE}📥 [3.5/5] Master サーバーから COEIROINK 話者モデルを同期中...${C_RESET}"
MODELS_DIR="${WORK_DIR}/coeiroink_models"
mkdir -p "${MODELS_DIR}"

AUTH_HEADER=()
if [ -n "${WORKER_SECRET}" ]; then
    AUTH_HEADER=(-H "X-Worker-Token: ${WORKER_SECRET}")
fi

MODELS_LIST_JSON=$(curl -sSL --max-time 15 "${AUTH_HEADER[@]}" "${MASTER_URL}/api/worker/models/list" 2>/dev/null || echo "")
if [ -n "$MODELS_LIST_JSON" ] && command -v python3 >/dev/null 2>&1; then
    MODEL_NAMES=$(python3 -c "
import json, sys
try:
    d = json.loads('''${MODELS_LIST_JSON}''')
    for m in d.get('models', []):
        print(f\"{m.get('folder_name')}|{m.get('speaker_name')}|{m.get('size_mb')}\")
except Exception:
    pass
")
    TOTAL_M=$(echo "$MODEL_NAMES" | grep -c . || echo "0")
    echo -e "  Master に登録されている COEIROINK モデル: ${TOTAL_M} 件"

    IDX=0
    echo "$MODEL_NAMES" | while IFS='|' read -r FNAME SNAME SIZE_MB; do
        [ -z "$FNAME" ] && continue
        IDX=$((IDX + 1))
        TARGET_FOLDER="${MODELS_DIR}/${FNAME}"

        # 厳密な同期判定 (metas.json, model配下の実モデルファイル, サイズ照合)
        IS_SYNCED="false"
        if [ -f "${TARGET_FOLDER}/metas.json" ] && [ -d "${TARGET_FOLDER}/model" ]; then
            WEIGHT_FILE=$(find "${TARGET_FOLDER}/model" -type f \( -name "*.pth" -o -name "*.onnx" \) 2>/dev/null | head -n 1)
            if [ -n "$WEIGHT_FILE" ]; then
                IS_SYNCED="true"
                if [ -n "${SIZE_MB}" ] && [ "${SIZE_MB}" != "0" ]; then
                    LOCAL_MB=$(du -sm "${TARGET_FOLDER}" 2>/dev/null | awk '{print $1}')
                    EXP_MIN_MB=$(python3 -c "import math; print(int(float('${SIZE_MB}') * 0.90))" 2>/dev/null || echo "0")
                    if [ -n "$LOCAL_MB" ] && [ "$EXP_MIN_MB" -gt 0 ] && [ "$LOCAL_MB" -lt "$EXP_MIN_MB" ]; then
                        IS_SYNCED="false"
                    fi
                fi
            fi
        fi

        if [ "$IS_SYNCED" = "true" ]; then
            echo -e "  [${IDX}/${TOTAL_M}] ✨ ${SNAME} (${SIZE_MB}MB) は同期済みです (スキップ)"
            continue
        fi

        # 不完全な残骸があればクリーンアップ
        if [ -d "${TARGET_FOLDER}" ]; then
            rm -rf "${TARGET_FOLDER}"
        fi

        echo -e "  [${IDX}/${TOTAL_M}] ${SNAME} (${SIZE_MB}MB) をダウンロード・展開中..."
        DOWNLOAD_URL="${MASTER_URL}/api/worker/models/download/${FNAME}"
        curl -sSL "${AUTH_HEADER[@]}" "${DOWNLOAD_URL}" | tar -xf - -C "${MODELS_DIR}"

        if [ -f "${TARGET_FOLDER}/metas.json" ] && [ -d "${TARGET_FOLDER}/model" ]; then
            echo -e "  [${IDX}/${TOTAL_M}] ✅ ${SNAME} の配置完了"
        else
            echo -e "  [${IDX}/${TOTAL_M}] ⚠️ ${SNAME} の展開に失敗しました"
            rm -rf "${TARGET_FOLDER}" 2>/dev/null || true
        fi
    done
else
    echo -e "  ⚠️ モデル一覧の取得をスキップしました (手動配置も可能です)。"
fi

# 4. TTS エンジン & Worker Agent の起動
echo -e "\n${C_BLUE}🚀 [4/5] TTS エンジンをビルド・起動中...${C_RESET}"
echo -e "${C_YELLOW}  ※ 初回ビルドはイメージのダウンロードがあるため数分かかります。${C_RESET}"
if [ "$HAS_GPU" = "true" ]; then
    echo -e "${C_CYAN}  ※ GPU モードのため COEIROINK GPU 版 (約3.5GB) のダウンロードと初回ビルドが行われます。${C_RESET}"
fi
docker compose -f "${WORK_DIR}/docker-compose.worker.yml" up -d --build

echo -e "\n${C_BLUE}🤖 [5/5] Worker Agent (自律管理プロセス) を起動中...${C_RESET}"
docker compose -f "${WORK_DIR}/docker-compose.agent.yml" up -d

# 起動待機 & ヘルスチェック
echo -e "${C_BLUE}⏳ エンジンの起動を待機中 (最大60秒)...${C_RESET}"
READY=0
for i in $(seq 1 60); do
    if curl -s http://127.0.0.1:50021/version > /dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 1
done

if [ "$READY" -eq 1 ]; then
    echo -e "${C_GREEN}✅ VOICEVOX エンジンが正常に稼働開始しました！${C_RESET}"
else
    echo -e "${C_YELLOW}⚠️  VOICEVOX の起動待機がタイムアウトしました。バックグラウンドで引き続き起動処理中です。${C_RESET}"
fi

# Agent ヘルスチェック
AGENT_READY=0
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:50030/health > /dev/null 2>&1; then
        AGENT_READY=1
        break
    fi
    sleep 1
done

if [ "$AGENT_READY" -eq 1 ]; then
    echo -e "${C_GREEN}✅ Worker Agent が起動しました！(ポート 50030)${C_RESET}"
else
    echo -e "${C_YELLOW}⚠️  Worker Agent の起動待機がタイムアウトしました。ログを確認してください:${C_RESET}"
    echo -e "   docker logs ttsworker-agent"
fi

# 5. 完了案内
# Master への自動登録確認
REGISTERED_OK=0
if [ "$AGENT_READY" -eq 1 ]; then
    echo -e "\n${C_BLUE}⏳ Master (${MASTER_URL}) への自動登録を確認中...${C_RESET}"
    for i in $(seq 1 15); do
        STATUS_JSON=$(curl -s --max-time 2 http://127.0.0.1:50030/status 2>/dev/null || echo "")
        if echo "$STATUS_JSON" | grep -q '"master_registered":\s*true'; then
            REGISTERED_OK=1
            break
        fi
        sleep 1
    done
fi

echo -e "\n${C_BOLD}${C_GREEN}"
echo "=============================================================================="
echo "   🎉 セットアップ完了！"
echo "=============================================================================="
echo -e "${C_RESET}"

echo -e "${C_BOLD}このワーカーの情報:${C_RESET}"
echo -e "  LAN IP アドレス : ${C_CYAN}${LOCAL_IP}${C_RESET}"
echo -e "  Worker Agent  : ${C_CYAN}http://${LOCAL_IP}:50030${C_RESET}"
echo -e "  VOICEVOX      : ${C_CYAN}http://${LOCAL_IP}:50021 $([ "$HAS_GPU" = "true" ] && echo '(GPU)' || echo '(CPU)')${C_RESET}"
echo -e "  SHAREVOX      : ${C_CYAN}http://${LOCAL_IP}:50025 $([ "$HAS_GPU" = "true" ] && echo '(GPU)' || echo '(CPU)')${C_RESET}"
echo -e "  COEIROINK     : ${C_CYAN}http://${LOCAL_IP}:50031 $([ "$HAS_GPU" = "true" ] && echo '(GPU)' || echo '(CPU)')${C_RESET}"

if [ "$REGISTERED_OK" -eq 1 ]; then
    echo -e "\n${C_BOLD}${C_GREEN}🎉 Master (${MASTER_URL}) への自動登録が完了しました！${C_RESET}"
    echo -e "${C_GREEN}   WebUI の「ワーカー管理」画面に本ノードが即座に反映されています。${C_RESET}"
    echo -e "${C_GREEN}   config.yaml への手動追加は一切不要です！${C_RESET}"
else
    echo -e "\n${C_BOLD}${C_CYAN}Worker Agent は Master (${MASTER_URL}) へ自動登録を継続実行しています。${C_RESET}"
    echo -e "${C_BLUE}   WebUI の「ワーカー管理」画面で接続状況をご確認ください。${C_RESET}"
fi

echo -e "\n${C_BOLD}※参考: オフライン等で手動設定する場合のみ config.yaml に追加する内容:${C_RESET}"
echo -e "${C_CYAN}-----------------------------------------------------------"
cat << EOF
  voicevox:
    nodes:
      - id: "linux_worker_${LOCAL_IP//./_}"
        url: "http://${LOCAL_IP}:50021"
        priority: 2
        max_concurrent: 1
        enabled: true
  sharevox:
    nodes:
      - id: "linux_worker_${LOCAL_IP//./_}"
        url: "http://${LOCAL_IP}:50025"
        priority: 2
        max_concurrent: 1
        enabled: true
  coeiroink:
    nodes:
      - id: "linux_worker_${LOCAL_IP//./_}"
        url: "http://${LOCAL_IP}:50031"
        priority: 2
        max_concurrent: 1
        enabled: true
EOF
echo -e "${C_RESET}"
echo -e "\n${C_BOLD}モデル同期 (COEIROINK, Masterサーバー側で実行):${C_RESET}"
echo -e "   ${C_YELLOW}rsync -avz --delete /path/to/coeiroink_models/ rsync://${LOCAL_IP}:8730/models/${C_RESET}\n"
echo -e "\n${C_BOLD}アップデート方法 (自動):${C_RESET}"
echo -e "   WebUI の Worker 管理ページから「アップデート」ボタンをクリック"
echo -e "   または: ${C_YELLOW}curl -X POST http://${LOCAL_IP}:50030/update${C_RESET}\n"
echo -e "${C_GREEN}完全自動化されました。セットアップはこれで終了です！ 🎉${C_RESET}\n"
