#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$ROOT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    printf '%s\n' "未找到 $PYTHON" >&2
    printf '%s\n' "请先运行: python3 -m venv .venv" >&2
    exit 1
fi

cd "$ROOT_DIR"
exec "$PYTHON" "$ROOT_DIR/app/main.py" "$@"
