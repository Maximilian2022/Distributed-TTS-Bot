#!/bin/bash
set -e

ENGINE_BIN="/opt/coeiroink/COEIROINK_LINUX_CPU_v.2025.4.1/engine/engine"
ENGINE_DIR="$(dirname "$ENGINE_BIN")"

cd "$ENGINE_DIR"

echo "Starting socat proxy (0.0.0.0:50031 -> 127.0.0.1:50032)..."
socat TCP-LISTEN:50031,fork,reuseaddr TCP:127.0.0.1:50032 &

echo "Starting COEIROINK Engine..."
exec "$ENGINE_BIN" "$@"
