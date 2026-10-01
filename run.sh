#!/usr/bin/env bash
# 一条本地命令启动系统：建库、载入延期与复议等演示案例、启动 Web 服务与提醒。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

PYTHON="${PYTHON:-python3}"
exec "$PYTHON" -m app --host "$HOST" --port "$PORT" "$@"
