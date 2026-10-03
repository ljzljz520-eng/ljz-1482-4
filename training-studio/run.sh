#!/usr/bin/env bash
# 一键启动：python3 run.sh [port] [db]
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"; DB="${2:-studio.db}"
exec python3 -m app.server --db "$DB" --port "$PORT"
