#!/usr/bin/env python3
"""NEURAL DECK —— 树莓派 5 + AI HAT+ 2 性能观测台，内置设备诊断智能体。

后端职责：
1. 每秒采集系统 / Hailo NPU / 本地 LLM 指标
2. 通过 WebSocket 推送给前端大屏
3. 提供一个基于本地大模型的最小 Agent（工具调用 + 结论生成）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

OLLAMA_BASE = os.environ.get("HUD_OLLAMA", "http://127.0.0.1:8000")
DEFAULT_MODEL = os.environ.get("HUD_MODEL", "qwen2.5-instruct:1.5b")

# ---------------------------------------------------------------- 全局状态

STATE: dict = {
    "metrics": {},
    "history": {
        "cpu": deque(maxlen=90),
        "mem": deque(maxlen=90),
        "npu": deque(maxlen=90),
        "soc": deque(maxlen=90),
        "rx": deque(maxlen=90),
        "tx": deque(maxlen=90),
    },
    "agent": {"requests": 0, "tokens": 0, "latency": 0.0},
}

# 静态 Hailo 信息（启动时读一次，之后不重复调用 CLI）
HAILO_STATIC: dict = {"arch": "HAILO10H", "fw": "-", "product": "AI HAT+ 2", "serial": "-"}
HAILO_TEMP: dict = {"value": None, "ts": 0.0, "busy": False}
LLM_CACHE: dict = {"models": [], "online": False, "ts": 0.0}
AGENT_BUSY = False  # Agent 推理期间暂停 NPU 探针，避免抢占设备


def _clean_env() -> dict:
    """去掉会污染 HailoRT 动态库的 IDE 环境变量。"""
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    return env


def read_hailo_static() -> dict:
    """用 CLI 读取固件/架构信息（不占用 NPU）。"""
    try:
        out = subprocess.run(
            ["hailortcli", "fw-control", "identify"],
            capture_output=True, text=True, timeout=10, env=_clean_env(),
        ).stdout
        pairs = re.findall(r"^([A-Za-z ]+):\s*(.+)$", out, re.MULTILINE)
        info = {k.strip(): v.strip() for k, v in pairs}
        return {
            "arch": info.get("Device Architecture", "HAILO10H").split()[0],
            "fw": info.get("Firmware Version", "-").split()[0],
            "product": info.get("Product Name", "AI HAT+ 2"),
            "serial": info.get("Serial Number", "-"),
        }
    except Exception:
        return dict(HAILO_STATIC)


def probe_hailo_temp() -> None:
    """读取 Hailo 芯片温度。与 LLM 服务同抢 NPU 时会失败，失败即标记 busy。"""
    global HAILO_TEMP
    try:
        from hailo_platform import Device  # 延迟导入，避免拖慢启动

        dev = Device()
        try:
            t = dev.control.get_chip_temperature()
            value = max(float(t.ts0_temperature), float(t.ts1_temperature))
            HAILO_TEMP = {"value": round(value, 1), "ts": time.time(), "busy": False}
            return
        finally:
            try:
                dev.release()
            except Exception:
                pass
    except Exception:
        HAILO_TEMP = {
            "value": HAILO_TEMP.get("value"),
            "ts": time.time(),
            "busy": True,
        }


def local_ip() -> str:
    """取局域网 IP（优先 wlan0 / eth0）。"""
    try:
        for name, addrs in psutil.net_if_addrs().items():
            if name.startswith("wl") or name.startswith("eth"):
                for a in addrs:
                    if a.family.name == "AF_INET" and not a.address.startswith("127."):
                        return a.address
    except Exception:
        pass
    return "0.0.0.0"


def build_metrics(prev: dict, last_net: dict) -> tuple[dict, dict, dict]:
    """采集一帧指标，返回 (metrics, last_net, last_disk)。"""
    now = time.time()

    # CPU
    cpu_pct = psutil.cpu_percent(None)
    per_core = psutil.cpu_percent(None, percpu=True)
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        load = [0, 0, 0]
    freq = None
    try:
        f = psutil.cpu_freq()
        freq = round(f.current) if f else None
    except Exception:
        pass

    # 内存
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # 温度（SoC）
    soc_temp = None
    try:
        soc_temp = round(psutil.sensors_temperatures()["cpu_thermal"][0].current, 1)
    except Exception:
        pass

    # 磁盘
    disk = psutil.disk_usage("/")
    dio = psutil.disk_io_counters()
    prev_dio = prev.get("disk_io") or {}
    d_read = dio.read_bytes - prev_dio.get("read_bytes", dio.read_bytes)
    d_write = dio.write_bytes - prev_dio.get("write_bytes", dio.write_bytes)

    # 网络
    nio = psutil.net_io_counters()
    rx_rate = max(0, nio.bytes_recv - last_net.get("bytes_recv", nio.bytes_recv))
    tx_rate = max(0, nio.bytes_sent - last_net.get("bytes_sent", nio.bytes_sent))

    # 进程
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = p.info
            mi = info.get("memory_info")
            procs.append({
                "pid": info["pid"],
                "name": (info["name"] or "?")[:16],
                "cpu": round(info.get("cpu_percent") or 0, 1),
                "mem": round((mi.rss if mi else 0) / 1048576, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: (x["cpu"], x["mem"]), reverse=True)

    hailo_temp = HAILO_TEMP.get("value")
    npu_age = now - HAILO_TEMP.get("ts", 0)

    metrics = {
        "ts": now,
        "uptime": int(now - psutil.boot_time()),
        "cpu": {"pct": round(cpu_pct, 1), "cores": [round(c, 1) for c in per_core], "load": load, "freq": freq},
        "mem": {
            "total_gb": round(mem.total / 1073741824, 1),
            "used_gb": round(mem.used / 1073741824, 1),
            "pct": round(mem.percent, 1),
            "swap_pct": round(swap.percent, 1),
        },
        "thermal": {"soc": soc_temp, "npu": hailo_temp},
        "disk": {
            "total_gb": round(disk.total / 1073741824, 1),
            "used_gb": round(disk.used / 1073741824, 1),
            "pct": round(disk.percent, 1),
            "read_rate": d_read,
            "write_rate": d_write,
        },
        "net": {"rx": rx_rate, "tx": tx_rate, "ip": local_ip()},
        "hailo": {
            "online": True,
            "arch": HAILO_STATIC.get("arch", "HAILO10H"),
            "fw": HAILO_STATIC.get("fw", "-"),
            "product": HAILO_STATIC.get("product", "AI HAT+ 2"),
            "temp": hailo_temp,
            "busy": HAILO_TEMP.get("busy", False) or npu_age > 8,
        },
        "llm": {
            "online": LLM_CACHE["online"],
            "models": LLM_CACHE["models"],
            "requests": STATE["agent"]["requests"],
            "tokens": STATE["agent"]["tokens"],
            "latency": STATE["agent"]["latency"],
        },
        "procs": procs[:10],
    }

    new_prev = {
        "disk_io": {
            "read_bytes": dio.read_bytes,
            "write_bytes": dio.write_bytes,
        }
    }
    return metrics, {"bytes_recv": nio.bytes_recv, "bytes_sent": nio.bytes_sent}, new_prev


async def fetch_llm_models() -> None:
    """查询本地 LLM 模型列表（每 10 秒一次）。"""
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{OLLAMA_BASE}/api/tags")
            data = r.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            LLM_CACHE.update({"models": models, "online": True, "ts": time.time()})
    except Exception:
        LLM_CACHE.update({"online": False, "ts": time.time()})


async def collector() -> None:
    """后台采集循环：1 秒一帧。"""
    psutil.cpu_percent(None)
    last_net: dict = {}
    prev: dict = {}
    last_hailo = 0.0
    last_llm = 0.0

    while True:
        try:
            metrics, last_net, prev = build_metrics(prev, last_net)
            now = time.time()

            if now - last_hailo > 12 and not AGENT_BUSY:
                await asyncio.to_thread(probe_hailo_temp)
                last_hailo = now

            if now - last_llm > 10:
                await fetch_llm_models()
                last_llm = now

            # 用最新温度回填（探针在采集之后才更新）
            metrics["thermal"]["npu"] = HAILO_TEMP.get("value")
            metrics["hailo"]["temp"] = HAILO_TEMP.get("value")
            metrics["hailo"]["busy"] = HAILO_TEMP.get("busy", False)

            h = STATE["history"]
            h["cpu"].append(metrics["cpu"]["pct"])
            h["mem"].append(metrics["mem"]["pct"])
            h["npu"].append(metrics["thermal"]["npu"] or 0)
            h["soc"].append(metrics["thermal"]["soc"] or 0)
            h["rx"].append(metrics["net"]["rx"] / 1024)
            h["tx"].append(metrics["net"]["tx"] / 1024)

            STATE["metrics"] = metrics
        except Exception as exc:  # 采集失败不影响服务存活
            STATE["metrics"] = {"ts": time.time(), "error": str(exc)}
        await asyncio.sleep(1)


# ---------------------------------------------------------------- 工具集（Agent 使用）

def tool_system() -> dict:
    m = STATE["metrics"]
    return {
        "cpu_pct": m.get("cpu", {}).get("pct"),
        "per_core": m.get("cpu", {}).get("cores"),
        "load": m.get("cpu", {}).get("load"),
        "freq_mhz": m.get("cpu", {}).get("freq"),
        "mem_pct": m.get("mem", {}).get("pct"),
        "mem_used_gb": m.get("mem", {}).get("used_gb"),
        "mem_total_gb": m.get("mem", {}).get("total_gb"),
        "soc_temp_c": m.get("thermal", {}).get("soc"),
        "uptime_s": m.get("uptime"),
        "disk_pct": m.get("disk", {}).get("pct"),
    }


def tool_processes() -> list:
    return STATE["metrics"].get("procs", [])[:8]


def tool_hailo() -> dict:
    m = STATE["metrics"]
    h = m.get("hailo", {})
    return {
        "online": h.get("online"),
        "arch": h.get("arch"),
        "firmware": h.get("fw"),
        "chip_temp_c": h.get("temp"),
        "busy": h.get("busy"),
        "note": "busy=true 表示 NPU 正被推理占用，或温度读取被占用冲突",
    }


def tool_llm() -> dict:
    m = STATE["metrics"]
    return m.get("llm", {})


def tool_network() -> dict:
    m = STATE["metrics"]
    return {
        "ip": m.get("net", {}).get("ip"),
        "rx_kbps": round(m.get("net", {}).get("rx", 0) / 1024, 1),
        "tx_kbps": round(m.get("net", {}).get("tx", 0) / 1024, 1),
    }


def tool_all() -> dict:
    return {
        "system": tool_system(),
        "hailo": tool_hailo(),
        "llm": tool_llm(),
        "network": tool_network(),
        "top_processes": tool_processes(),
    }


TOOLS = {
    "system": tool_system,
    "processes": tool_processes,
    "hailo": tool_hailo,
    "llm": tool_llm,
    "network": tool_network,
    "all": tool_all,
}

PLAN_PROMPT = """你是 LaughingPi（树莓派 5 + Hailo AI HAT+ 2）的设备诊断智能体。
你只能使用下列工具获取真实数据，禁止编造数字。

工具：
- system：CPU / 内存 / 温度 / 负载 / 磁盘
- processes：占用最高的进程
- hailo：NPU 状态与芯片温度
- llm：本地大模型服务与模型列表
- network：网络吞吐与 IP
- all：以上全部

任务：根据用户问题选择最合适的一个工具。
严格只输出一行 JSON，不要解释、不要代码块、不要多余内容。
格式示例：
{"tool":"system"}
{"tool":"all"}
"""

FINAL_PROMPT = """你是设备诊断播报员。下面是已核实的数据与结论，必须原样保留结论，禁止自行比较阈值。
输出 2-3 句中文纯文本，不要标题、不要编号、不要照抄这段指令、不要出现"第一句"等字样：
1) 先说有无风险；
2) 引用两个最关键数值（数字只能来自上面的数据，禁止新增或改动）；
3) 给一条建议（无风险则省略）。
"""


def parse_action(text: str) -> dict | None:
    """从模型输出里抽出第一个 JSON 对象。"""
    match = re.search(r"\{[\s\S]*?\}", text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if "tool" in obj and obj["tool"] in TOOLS:
        return {"type": "tool", "name": obj["tool"]}
    if "final" in obj and isinstance(obj["final"], str) and obj["final"].strip():
        return {"type": "final", "text": obj["final"].strip()}
    return None


async def ollama_chat(messages: list, model: str, timeout: float = 40,
                      temperature: float = 0.0, num_predict: int = 120) -> str:
    """非流式对话。"""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5)) as c:
        r = await c.post(f"{OLLAMA_BASE}/api/chat", json=payload)
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content", "").strip()


async def ollama_stream(messages: list, model: str):
    """流式对话，逐 token yield。"""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": 0.6, "num_predict": 150},
    }
    timeout = httpx.Timeout(180.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream("POST", f"{OLLAMA_BASE}/api/chat", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message") or {}
                chunk = msg.get("content")
                if chunk:
                    yield chunk
                if obj.get("done"):
                    break


def rule_based_answer(obs: dict) -> str:
    """模型不可用时的兜底结论。"""
    s = obs.get("system", {})
    h = obs.get("hailo", {})
    parts = [
        f"CPU {s.get('cpu_pct', '?')}%",
        f"内存 {s.get('mem_pct', '?')}%",
        f"SoC {s.get('soc_temp_c', '?')}°C",
    ]
    if h.get("chip_temp_c"):
        parts.append(f"NPU {h['chip_temp_c']}°C")
    text = "当前状态：" + "，".join(parts) + "。"
    if (s.get("soc_temp_c") or 0) > 75:
        text += " SoC 温度偏高，建议检查散热风扇。"
    if (s.get("mem_pct") or 0) > 90:
        text += " 内存接近饱和，建议释放进程。"
    return text


PREFERRED_MODELS = ["llama3.2:3b", "qwen2.5-instruct:1.5b", "qwen2:1.5b"]


def pick_model() -> str:
    """优先选指令能力更好的小模型；deepseek_r1 推理模型太慢，不作为首选。"""
    for name in PREFERRED_MODELS:
        if name in LLM_CACHE["models"]:
            return name
    return LLM_CACHE["models"][0] if LLM_CACHE["models"] else DEFAULT_MODEL


def route_keyword(question: str) -> str:
    """模型规划失败时的关键词兜底路由，保证 Agent 永远可用。"""
    rules = [
        ("hailo", ("npu", "神经网络", "芯片", "hailo")),
        ("processes", ("进程", "占用", "什么在跑")),
        ("network", ("网络", "带宽", "ip", "延迟")),
        ("llm", ("模型", "llm", "大模型", "ollama")),
        ("system", ("内存", "cpu", "磁盘", "负载")),
    ]
    q = question.lower()
    for name, keys in rules:
        if any(k in q for k in keys):
            return name
    return "all"


def summarize_obs(obs: dict) -> str:
    """把嵌套遥测压成中文短句，降低小模型的解析负担。"""
    lines: list[str] = []
    s = obs.get("system")
    if s:
        lines.append(f"CPU {s.get('cpu_pct')}%（每核 {s.get('per_core')}，{s.get('freq_mhz')}MHz），负载 {s.get('load')}")
        lines.append(f"内存 {s.get('mem_used_gb')}/{s.get('mem_total_gb')}GB，占用 {s.get('mem_pct')}%")
        lines.append(f"SoC 温度 {s.get('soc_temp_c')}°C，磁盘占用 {s.get('disk_pct')}%")
    h = obs.get("hailo")
    if h:
        lines.append(f"NPU {h.get('arch')} 固件 {h.get('firmware')}，芯片温度 {h.get('chip_temp_c')}°C，推理占用中={h.get('busy')}")
    l = obs.get("llm")
    if l:
        lines.append(f"本地大模型服务{'在线' if l.get('online') else '离线'}，共 {len(l.get('models') or [])} 个模型")
    n = obs.get("network")
    if n:
        lines.append(f"网络 IP {n.get('ip')}，下行 {n.get('rx_kbps')}KB/s，上行 {n.get('tx_kbps')}KB/s")
    p = obs.get("processes") or obs.get("top_processes")
    if isinstance(p, list) and p:
        top = "、".join(f"{x['name']}({x['cpu']}%)" for x in p[:3])
        lines.append(f"CPU 占用最高的进程：{top}")
    return "\n".join(lines) if lines else json.dumps(obs, ensure_ascii=False)[:600]


def detect_risks(obs: dict) -> list[str]:
    """代码侧先算风险，避免小模型误判。"""
    risks = []
    s = obs.get("system") or {}
    h = obs.get("hailo") or {}
    if (s.get("soc_temp_c") or 0) > 75:
        risks.append(f"SoC 温度 {s['soc_temp_c']}°C 超过 75°C，建议检查散热")
    if (s.get("soc_temp_c") or 0) > 85:
        risks.append("SoC 温度超过 85°C，存在降频或死机风险")
    if (s.get("mem_pct") or 0) > 90:
        risks.append(f"内存占用 {s['mem_pct']}% 接近饱和")
    if (s.get("disk_pct") or 0) > 90:
        risks.append(f"磁盘占用 {s['disk_pct']}% 接近写满")
    if (h.get("chip_temp_c") or 0) > 80:
        risks.append(f"NPU 芯片温度 {h['chip_temp_c']}°C 偏高")
    return risks


async def run_agent(question: str, send, model_override: str = "") -> None:
    """最小 Agent：规划 → 调工具 → 生成结论（流式）。"""
    global AGENT_BUSY
    t0 = time.time()
    AGENT_BUSY = True
    model = model_override or pick_model()
    obs: dict = {}

    try:
        await send({"type": "log", "text": f"◤ 链路建立 · 模型 {model}"})

        action = None
        try:
            raw = await ollama_chat(
                [{"role": "system", "content": PLAN_PROMPT},
                 {"role": "user", "content": question}],
                model, timeout=60, num_predict=30,
            )
            action = parse_action(raw)
        except Exception as exc:
            await send({"type": "log", "text": f"⚠ 规划无响应（{type(exc).__name__}），全量采集"})

        name = action["name"] if action and action["type"] == "tool" else route_keyword(question)
        await send({"type": "log", "text": f"▸ 调用工具 [{name}]"})
        try:
            raw_obs = await asyncio.to_thread(TOOLS[name])
            if name == "all":
                obs = raw_obs if isinstance(raw_obs, dict) else {"result": raw_obs}
            else:
                obs = {name: raw_obs}
        except Exception as exc:
            obs = {"error": str(exc)}
        await send({"type": "log", "text": "✓ 遥测数据已回传"})

        # 如果模型直接给了结论，直接流式下发
        if action and action["type"] == "final":
            for ch in action["text"]:
                await send({"type": "token", "text": ch})
            await send({"type": "done"})
            return

        await send({"type": "log", "text": "◈ 生成诊断结论"})
        summary = summarize_obs(obs)
        risks = detect_risks(obs)
        risk_text = "；".join(risks) if risks else "未发现阈值风险"
        print(f"[agent] {summary} | risks: {risk_text}", flush=True)
        messages = [
            {"role": "system", "content": FINAL_PROMPT},
            {"role": "user", "content":
                f"用户问题：{question}\n\n实时数据：\n{summary}\n\n代码判定的风险：{risk_text}"},
        ]
        emitted = False
        try:
            async for chunk in ollama_stream(messages, model):
                emitted = True
                STATE["agent"]["tokens"] += 1
                await send({"type": "token", "text": chunk})
        except Exception as exc:
            await send({"type": "log", "text": f"⚠ 生成中断：{type(exc).__name__}"})

        if not emitted:
            text = rule_based_answer(obs) if "system" in obs else "遥测数据已采集，但模型当前不可用。"
            for ch in text:
                await send({"type": "token", "text": ch})

        STATE["agent"]["requests"] += 1
        STATE["agent"]["latency"] = round(time.time() - t0, 2)
        await send({"type": "done"})
    finally:
        AGENT_BUSY = False


# ---------------------------------------------------------------- 应用

async def warmup_llm() -> None:
    """启动后预热模型，避免用户第一次提问时等模型加载。"""
    try:
        await asyncio.sleep(3)
        model = pick_model()
        await ollama_chat([{"role": "user", "content": "ok"}], model, timeout=120, num_predict=2)
        print(f"[warmup] {model} ready")
    except Exception as exc:
        print(f"[warmup] skipped: {type(exc).__name__}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    HAILO_STATIC.update(await asyncio.to_thread(read_hailo_static))
    await fetch_llm_models()
    task = asyncio.create_task(collector())
    warm = asyncio.create_task(warmup_llm())
    try:
        yield
    finally:
        task.cancel()
        warm.cancel()


app = FastAPI(title="NEURAL DECK", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/metrics")
async def api_metrics():
    return {"metrics": STATE["metrics"], "history": {k: list(v) for k, v in STATE["history"].items()}}


@app.get("/api/health")
async def api_health():
    return {"ok": True, "ts": time.time()}


@app.websocket("/ws/metrics")
async def ws_metrics(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json({
                "metrics": STATE["metrics"],
                "history": {k: list(v) for k, v in STATE["history"].items()},
            })
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            question = str(data.get("question", "")).strip()
            model = str(data.get("model", "") or "")
            if model not in LLM_CACHE["models"]:
                model = ""
            if not question:
                await ws.send_json({"type": "log", "text": "⚠ 空指令"})
                await ws.send_json({"type": "done"})
                continue
            await run_agent(question, ws.send_json, model)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await ws.send_json({"type": "log", "text": f"✕ 会话异常：{type(exc).__name__}"})
            await ws.send_json({"type": "done"})
        except Exception:
            pass


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
