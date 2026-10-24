# ==============================================================================
# TTSBot Multi-stage Dockerfile
# Stage 1: Builder (Cコンパイラ・ヘッダー等を備えてwheelをビルド)
# Stage 2: Runtime (実行に必要な共有ライブラリとPython成果物のみの最小イメージ)
# ==============================================================================

# --- Stage 1: Builder ---
FROM python:3.14-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# C拡張やビルドに必要な開発パッケージを導入
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libopus-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt requirements-optional.txt ./
# --prefix=/install に一括インストール (コア依存 + オプショナル拡張)
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt -r requirements-optional.txt


# --- Stage 2: Runtime ---
FROM python:3.14-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    TZ=Asia/Tokyo

# ランタイムに必要なパッケージのみをインストール (gcc, python3-dev, libopus-dev等のビルドツールは排除)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    open-jtalk \
    open-jtalk-mecab-naist-jdic \
    ca-certificates \
    tzdata \
    zstd \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# BuilderステージでビルドしたPythonパッケージを /usr/local へ転送
COPY --from=builder /install /usr/local

WORKDIR /app

# ディレクトリ作成
RUN mkdir -p /app/cache /app/cache_voicevox /app/coeiroink_models /app/data /app/logs /app/src /app/openjtalk_voices /app/scripts

# アプリケーションコードおよび音声データのコピー
COPY config.yaml .
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY openjtalk_voices/ ./openjtalk_voices/

# ボットの起動
CMD ["python", "-m", "src.main"]

