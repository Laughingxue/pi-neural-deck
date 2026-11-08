#!/usr/bin/env bash
# 验证 start.sh：静态检查 -> 启动 -> HTTP/WS 探活 -> 模拟 Ctrl+C -> 确认清理
set -uo pipefail

cd "$(dirname "$0")"

PASS=0
FAIL=0
ok()  { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

LOG=$(mktemp /tmp/neural-deck-test.XXXXXX.log)

# 与 start.sh 相同的 Python 选择逻辑
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif [ -x ../.venv/bin/python ]; then
  PY=../.venv/bin/python
else
  PY=python3
fi

echo "== 1. 静态检查 =="
bash -n start.sh && ok "语法检查 (bash -n)" || bad "语法检查"
[ -x start.sh ] && ok "start.sh 可执行" || bad "start.sh 缺少执行权限"
grep -q 'unset LD_LIBRARY_PATH LD_PRELOAD' start.sh && ok "清理 HailoRT 动态库环境" || bad "缺少 LD_LIBRARY_PATH/LD_PRELOAD 清理"
grep -q 'hailo-ollama' start.sh && ok "包含 hailo-ollama 拉起逻辑" || bad "缺少 hailo-ollama 拉起逻辑"

echo "== 2. 启动 start.sh =="
# 记录 8000 是否本来就在运行，清理阶段据此判断归属
PRE_OLLAMA=0
curl -sf --max-time 1 http://127.0.0.1:8000/api/tags >/dev/null 2>&1 && PRE_OLLAMA=1

# setsid 让脚本自成一个进程组，便于模拟 Ctrl+C 时整组收到信号
setsid ./start.sh >"$LOG" 2>&1 &
SPID=$!

cleanup() {
  kill -INT -- "-$SPID" 2>/dev/null || true
  sleep 1
  kill -KILL -- "-$SPID" 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT

# 3. 等待 HTTP 健康检查
HTTP_CODE=""
for _ in $(seq 1 60); do
  HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 http://127.0.0.1:8080/api/health 2>/dev/null || true)
  [ "$HTTP_CODE" = "200" ] && break
  kill -0 "$SPID" 2>/dev/null || break
  sleep 0.5
done
[ "$HTTP_CODE" = "200" ] && ok "HTTP 服务就绪 (/api/health -> 200)" || bad "HTTP 服务未就绪 (最后状态: ${HTTP_CODE:-无})"

# 4. 指标接口返回真实数据
if curl -sf --max-time 3 http://127.0.0.1:8080/api/metrics 2>/dev/null | grep -q '"cpu"'; then
  ok "/api/metrics 返回指标数据"
else
  bad "/api/metrics 无有效数据"
fi

# 5. WebSocket 推送
if "$PY" - <<'PY' >/dev/null 2>&1
import asyncio, json
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8080/ws/metrics", open_timeout=5) as ws:
        data = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        metrics = data.get("metrics", {})
        assert "cpu" in metrics and "mem" in metrics

asyncio.run(main())
PY
then
  ok "/ws/metrics 可连接并收到指标帧"
else
  bad "/ws/metrics 连接或数据帧失败"
fi

# 6. hailo-ollama 自动拉起
OLLAMA_UP=0
for _ in $(seq 1 60); do
  if curl -sf --max-time 1 http://127.0.0.1:8000/api/tags >/dev/null 2>&1; then
    OLLAMA_UP=1
    break
  fi
  kill -0 "$SPID" 2>/dev/null || break
  sleep 0.5
done
[ "$OLLAMA_UP" = "1" ] && ok "hailo-ollama 已就绪 (:8000)" || bad "hailo-ollama 未就绪 (:8000)"

# 7. 模拟 Ctrl+C：向整个进程组发 SIGINT
kill -INT -- "-$SPID" 2>/dev/null
STOPPED=0
for _ in $(seq 1 40); do
  if ! kill -0 "$SPID" 2>/dev/null; then
    STOPPED=1
    break
  fi
  sleep 0.5
done
[ "$STOPPED" = "1" ] && ok "start.sh 进程已退出" || bad "start.sh 进程未退出"

# 8. 端口释放
PORT_FREE=1
for _ in $(seq 1 30); do
  if curl -sf --max-time 1 http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    PORT_FREE=0
    sleep 0.5
  else
    PORT_FREE=2
    break
  fi
done
[ "$PORT_FREE" = "2" ] && ok "8080 端口已释放" || bad "8080 仍在监听"

if [ "$PRE_OLLAMA" = "0" ]; then
  if curl -sf --max-time 1 http://127.0.0.1:8000/api/tags >/dev/null 2>&1; then
    bad "脚本拉起的 hailo-ollama 未随退出关闭"
  else
    ok "hailo-ollama 已随退出关闭"
  fi
else
  echo "  [SKIP] 启动前 hailo-ollama 已在运行，不校验其关闭"
fi

# 9. 输出日志
echo "== 启动日志 =="
sed -e 's/^/  /' "$LOG" | tail -12

echo
echo "结果: $PASS 通过, $FAIL 失败"
[ "$FAIL" -eq 0 ]
