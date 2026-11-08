#!/usr/bin/env bash
# NEURAL DECK 启动脚本：按需拉起 hailo-ollama，再启动仪表盘
set -euo pipefail

cd "$(dirname "$0")"

# 清理 IDE 注入的库路径，避免 HailoRT 动态库冲突
unset LD_LIBRARY_PATH LD_PRELOAD

# 选择 Python 环境：优先项目内 .venv，其次上级目录 .venv，最后系统 python3
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif [ -x ../.venv/bin/python ]; then
  PY=../.venv/bin/python
else
  PY=python3
fi

OLLAMA_PID=""
cleanup() {
  if [ -n "$OLLAMA_PID" ] && kill -0 "$OLLAMA_PID" 2>/dev/null; then
    echo
    echo "[stop] 关闭 hailo-ollama (pid $OLLAMA_PID)"
    kill "$OLLAMA_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# hailo-ollama 未运行时在后台拉起
if command -v hailo-ollama >/dev/null 2>&1 &&
   ! curl -sf --max-time 2 http://127.0.0.1:8000/api/tags >/dev/null 2>&1; then
  echo "[start] 拉起 hailo-ollama ..."
  hailo-ollama >/tmp/hailo-ollama.log 2>&1 &
  OLLAMA_PID=$!
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "[start] NEURAL DECK -> http://${IP:-127.0.0.1}:8080   (Ctrl+C 退出)"
"$PY" app.py
