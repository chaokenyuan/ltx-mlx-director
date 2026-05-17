#!/usr/bin/env bash
# 啟動 LTX-2.3 Director UI（Gradio）
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x "$HOME/ltx-2-mlx/.venv/bin/ltx-2-mlx" ]; then
  echo "找不到 ltx-2-mlx，請先在 ~/ltx-2-mlx 跑 uv sync --all-extras" >&2
  exit 1
fi

exec uv run --with "gradio>=4.36" --with pandas python ui/app.py
