#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

if [ -n "${PYTHON_BIN:-}" ]; then
    PYTHON=$PYTHON_BIN
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
else
    PYTHON=python3
fi

exec "$PYTHON" -m gunicorn \
    --config "$PROJECT_DIR/gunicorn.conf.py" \
    --worker-class uvicorn_worker.UvicornWorker \
    --workers 1 \
    --threads 1 \
    --bind 127.0.0.1:3399 \
    backend.app.main:app
