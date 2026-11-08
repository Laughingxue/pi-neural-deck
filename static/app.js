/* NEURAL DECK —— 前端渲染与智能体交互 */

const $ = (id) => document.getElementById(id);
const CIRC = 2 * Math.PI * 52;

let latest = null;
let history = { cpu: [], mem: [], npu: [], soc: [], rx: [], tx: [] };
let agentBusy = false;

/* ---------------------------------------------------- 工具函数 */

function fmtBytes(n) {
  if (!n && n !== 0) return "--";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 1 : 0)}${u[i]}`;
}

function fmtUptime(s) {
  if (!s && s !== 0) return "--";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d > 0 ? `${d}D ${h}H ${m}M` : `${h}H ${m}M`;
}

function setChip(el, state, label) {
  el.className = "chip" + (state ? " " + state : "");
  if (label) el.innerHTML = `<i></i>${label}`;
}

/* ---------------------------------------------------- 背景星空 */

const bg = $("bg");
const bgCtx = bg.getContext("2d");
let stars = [];

function resizeBg() {
  const dpr = window.devicePixelRatio || 1;
  bg.width = innerWidth * dpr;
  bg.height = innerHeight * dpr;
  bg.style.width = innerWidth + "px";
  bg.style.height = innerHeight + "px";
  bgCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  stars = Array.from({ length: 90 }, () => ({
    x: Math.random() * innerWidth,
    y: Math.random() * innerHeight,
    z: Math.random() * 0.8 + 0.2,
    s: Math.random() * 1.6 + 0.4,
  }));
}

function drawBg(t) {
  bgCtx.clearRect(0, 0, innerWidth, innerHeight);

  // 网格
  bgCtx.strokeStyle = "rgba(34,230,255,0.055)";
  bgCtx.lineWidth = 1;
  const step = 48;
  for (let x = 0; x <= innerWidth; x += step) {
    bgCtx.beginPath(); bgCtx.moveTo(x, 0); bgCtx.lineTo(x, innerHeight); bgCtx.stroke();
  }
  for (let y = 0; y <= innerHeight; y += step) {
    bgCtx.beginPath(); bgCtx.moveTo(0, y); bgCtx.lineTo(innerWidth, y); bgCtx.stroke();
  }

  // 星点
  for (const st of stars) {
    st.y -= st.z * 0.35;
    if (st.y < -4) { st.y = innerHeight + 4; st.x = Math.random() * innerWidth; }
    const a = 0.25 + 0.35 * Math.abs(Math.sin(t / 900 + st.x));
    bgCtx.fillStyle = `rgba(140,235,255,${a})`;
    bgCtx.beginPath();
    bgCtx.arc(st.x, st.y, st.s, 0, Math.PI * 2);
    bgCtx.fill();
  }

  // 顶部扫描光带
  const gy = (t / 18) % (innerHeight + 240) - 120;
  const grad = bgCtx.createLinearGradient(0, gy - 90, 0, gy + 90);
  grad.addColorStop(0, "rgba(34,230,255,0)");
  grad.addColorStop(0.5, "rgba(34,230,255,0.045)");
  grad.addColorStop(1, "rgba(34,230,255,0)");
  bgCtx.fillStyle = grad;
  bgCtx.fillRect(0, gy - 90, innerWidth, 180);
}

/* ---------------------------------------------------- 画布适配 */

function fitCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 100;
  const h = cv.clientHeight || 100;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

/* ---------------------------------------------------- 雷达 */

const radar = $("radar");
let sweep = 0;
const blips = Array.from({ length: 7 }, (_, i) => ({
  a: (i / 7) * Math.PI * 2 + 0.6,
  r: 0.35 + Math.random() * 0.55,
  ph: Math.random() * Math.PI * 2,
}));

function drawRadar(t) {
  const { ctx, w, h } = fitCanvas(radar);
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2;
  const R = Math.min(w, h) / 2 - 8;
  const load = latest ? latest.cpu.pct / 100 : 0.2;
  const temp = latest && latest.thermal.npu ? latest.thermal.npu : 0;

  // 同心环
  for (let i = 1; i <= 3; i++) {
    ctx.beginPath();
    ctx.strokeStyle = `rgba(34,230,255,${0.08 + i * 0.03})`;
    ctx.lineWidth = 1;
    ctx.arc(cx, cy, (R * i) / 3, 0, Math.PI * 2);
    ctx.stroke();
  }

  // 十字线
  ctx.strokeStyle = "rgba(34,230,255,0.12)";
  ctx.beginPath();
  ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
  ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
  ctx.stroke();

  // 扫描扇形
  sweep += 0.022;
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(sweep);
  for (let i = 0; i < 26; i++) {
    ctx.beginPath();
    ctx.strokeStyle = `rgba(34,230,255,${(1 - i / 26) * 0.4})`;
    ctx.lineWidth = 1.2;
    ctx.moveTo(0, 0);
    const a0 = -i * 0.022, a1 = -(i + 1) * 0.022;
    ctx.lineTo(Math.cos(a0) * R, Math.sin(a0) * R);
    ctx.lineTo(Math.cos(a1) * R, Math.sin(a1) * R);
    ctx.stroke();
  }
  ctx.restore();

  // 光点
  for (const b of blips) {
    const diff = Math.abs(((sweep % (Math.PI * 2)) - b.a + Math.PI * 2) % (Math.PI * 2));
    const near = diff < 0.9 ? 1 - diff / 0.9 : 0;
    const alpha = 0.12 + near * 0.8 * (0.4 + load * 0.6);
    const x = cx + Math.cos(b.a) * R * b.r;
    const y = cy + Math.sin(b.a) * R * b.r;
    ctx.beginPath();
    ctx.fillStyle = `rgba(${temp > 75 ? "255,46,136" : "34,230,255"},${alpha})`;
    ctx.arc(x, y, 2.4 + near * 2, 0, Math.PI * 2);
    ctx.fill();
  }

  // 中心核心
  ctx.beginPath();
  ctx.fillStyle = `rgba(34,230,255,${0.25 + load * 0.5})`;
  ctx.arc(cx, cy, 3.5 + Math.sin(t / 300) * 1.2, 0, Math.PI * 2);
  ctx.fill();

  // 外圈提示
  ctx.beginPath();
  ctx.strokeStyle = "rgba(34,230,255,0.35)";
  ctx.lineWidth = 1;
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.stroke();
}

/* ---------------------------------------------------- 折线图 */

function drawLineChart(cv, series, opts) {
  const { ctx, w, h } = fitCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  const pad = 4;
  const n = opts.len || 60;

  ctx.strokeStyle = "rgba(34,230,255,0.08)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = pad + ((h - pad * 2) * i) / 4;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  for (const s of series) {
    const data = s.data.slice(-n);
    if (!data.length) continue;
    ctx.beginPath();
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.6;
    ctx.shadowColor = s.color;
    ctx.shadowBlur = 6;
    data.forEach((v, i) => {
      const x = (i / Math.max(1, n - 1)) * w;
      const ratio = Math.min(1, Math.max(0, v / s.max));
      const y = h - pad - ratio * (h - pad * 2);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.shadowBlur = 0;

    // 末端点
    const last = data[data.length - 1];
    const lx = w, ly = h - pad - Math.min(1, Math.max(0, last / s.max)) * (h - pad * 2);
    ctx.beginPath();
    ctx.fillStyle = s.color;
    ctx.arc(lx - 1, ly, 2, 0, Math.PI * 2);
    ctx.fill();
  }
}

function drawTrace() {
  const n = Math.max(history.cpu.length, 30);
  drawLineChart($("trace"), [
    { data: history.cpu, color: "#22e6ff", max: 100 },
    { data: history.soc || [], color: "#ff2e88", max: 100 },
  ], { len: n });
}

function drawSpark() {
  const n = Math.max(history.rx.length, 30);
  const maxRx = Math.max(64, ...history.rx.slice(-n), ...history.tx.slice(-n));
  drawLineChart($("spark"), [
    { data: history.rx, color: "#22e6ff", max: maxRx },
    { data: history.tx, color: "#2bffb0", max: maxRx },
  ], { len: n });
}

/* ---------------------------------------------------- 指标渲染 */

function makeCores() {
  const host = $("cores");
  if (host.children.length) return;
  for (let i = 0; i < 4; i++) {
    const d = document.createElement("div");
    d.className = "core";
    d.innerHTML = `<i>${i}</i><div class="bar"><u></u></div>`;
    host.appendChild(d);
  }
}

function buildProcs(procs) {
  const host = $("procs");
  const body = host.parentElement;
  const head = '<div class="proc-row head"><span>PID</span><span>PROCESS</span><span class="num">CPU</span><span class="num">MEM</span></div>';
  host.innerHTML = head;
  if (!body) return;
  const avail = body.clientHeight;
  for (const p of procs) {
    const d = document.createElement("div");
    d.className = "proc-row";
    d.innerHTML = `<span>${p.pid}</span><span title="${p.name}">${p.name}</span><span class="num">${p.cpu}%</span><span class="num">${p.mem}M</span>`;
    host.appendChild(d);
    if (host.scrollHeight > avail) {
      host.removeChild(d);
      break;
    }
  }
}

function render(m) {
  if (!m || !m.cpu) return;
  latest = m;

  // CPU
  const cpu = m.cpu;
  $("cpu-pct").textContent = cpu.pct + "%";
  $("cpu-big").textContent = Math.round(cpu.pct);
  $("cpu-ring").style.strokeDasharray = CIRC;
  $("cpu-ring").style.strokeDashoffset = CIRC * (1 - cpu.pct / 100);
  $("cpu-load").textContent = "LOAD " + (cpu.load || []).join(" / ");
  $("cpu-freq").textContent = (cpu.freq || "--") + " MHz";

  makeCores();
  const coreEls = $("cores").children;
  (cpu.cores || []).slice(0, 4).forEach((v, i) => {
    if (coreEls[i]) coreEls[i].querySelector("u").style.width = Math.min(100, v) + "%";
  });

  // 内存
  const mem = m.mem;
  $("mem-pct").textContent = mem.pct + "%";
  $("mem-big").textContent = Math.round(mem.pct);
  $("mem-ring").style.strokeDasharray = CIRC;
  $("mem-ring").style.strokeDashoffset = CIRC * (1 - mem.pct / 100);
  $("mem-used").textContent = mem.used_gb;
  $("mem-total").textContent = mem.total_gb;
  $("mem-swap").textContent = mem.swap_pct;
  const memState = $("mem-state");
  memState.textContent = mem.pct > 90 ? "CRITICAL" : mem.pct > 75 ? "HIGH" : "NOMINAL";
  memState.style.color = mem.pct > 90 ? "#ff2e88" : mem.pct > 75 ? "#ffb020" : "";

  // 温度
  const soc = m.thermal.soc;
  const npuT = m.thermal.npu;
  $("soc-temp").textContent = (soc ?? "--") + "°C";
  $("soc-big").textContent = soc ?? "--";
  $("npu-temp-sm").textContent = npuT ?? "--";
  const tp = Math.min(100, ((soc || 0) / 100) * 100);
  $("temp-bar").style.width = tp + "%";
  const tmeter = $("temp-meter");
  tmeter.className = "meter" + (soc > 80 ? " hot" : soc > 70 ? " warm" : "");
  const tstate = $("temp-state");
  tstate.textContent = soc > 80 ? "HOT" : soc > 70 ? "WARM" : "COOL";
  tstate.style.color = soc > 80 ? "#ff2e88" : soc > 70 ? "#ffb020" : "";

  // 磁盘
  const dk = m.disk;
  $("disk-pct").textContent = dk.pct + "%";
  $("disk-big").textContent = Math.round(dk.pct);
  $("disk-bar").style.width = Math.min(100, dk.pct) + "%";
  $("disk-meter").className = "meter" + (dk.pct > 90 ? " hot" : dk.pct > 80 ? " warm" : "");
  $("disk-used").textContent = dk.used_gb;
  $("disk-total").textContent = dk.total_gb;
  $("disk-io").textContent = `R ${fmtBytes(dk.read_rate)}/s · W ${fmtBytes(dk.write_rate)}/s`;

  // NPU
  const hp = m.hailo || {};
  $("npu-arch").textContent = hp.arch || "--";
  $("npu-fw").textContent = hp.fw || "--";
  $("npu-product").textContent = hp.product || "--";
  const ntBig = $("npu-temp-big");
  ntBig.textContent = (hp.temp ?? "--") + "°C";
  ntBig.className = "temp" + (hp.temp > 75 ? " hot" : "");
  $("npu-state").textContent = hp.online ? (hp.busy ? "BUSY" : "LINK") : "OFFLINE";
  $("npu-hint").textContent = hp.busy ? "NPU ENGAGED" : "SWEEPING";
  setChip($("chip-npu"), hp.online ? "on" : "warn", hp.online ? "NPU LINK" : "NPU OFFLINE");

  // LLM
  const ll = m.llm || {};
  setChip($("chip-llm"), ll.online ? "on" : "warn", ll.online ? "LLM ENGINE" : "LLM OFFLINE");
  $("llm-state").textContent = ll.online ? "READY" : "OFFLINE";
  $("llm-req").textContent = ll.requests ?? 0;
  $("llm-tok").textContent = ll.tokens ?? 0;
  $("llm-lat").textContent = ll.latency ? ll.latency + "s" : "--";
  $("llm-count").textContent = (ll.models || []).length + " MODELS";
  const chips = $("llm-models");
  const key = (ll.models || []).join(",");
  if (chips.dataset.key !== key) {
    chips.dataset.key = key;
    chips.innerHTML = (ll.models || []).map((n) => `<span class="mchip">${n}</span>`).join("") ||
      '<span class="mchip" style="opacity:.5">无可用模型</span>';
  }
  syncModels(ll.models || []);
  // 网络
  $("net-ip").textContent = (m.net && m.net.ip) || "--";
  $("net-rx").textContent = "RX " + fmtBytes(m.net.rx) + "/s";
  $("net-tx").textContent = "TX " + fmtBytes(m.net.tx) + "/s";

  // 进程：逐行填充到面板实际高度，窗口/浏览器缩放都自适应且不会裁切
  buildProcs(m.procs || []);

  setChip($("chip-pi"), "on", "PI CORE");

  // 底栏
  $("f-uptime").textContent = fmtUptime(m.uptime);
  $("f-status").textContent = "SYNC " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

const PREFERRED = ["llama3.2:3b", "qwen2.5-instruct:1.5b", "qwen2:1.5b"];
let modelKey = "";

function syncModels(models) {
  const sel = $("agent-model-select");
  const key = models.join(",");
  if (key === modelKey) return;
  modelKey = key;
  const prev = sel.value;
  sel.innerHTML = models.map((n) => `<option value="${n}">${n}</option>`).join("") ||
    '<option value="">--</option>';
  const pref = PREFERRED.find((n) => models.includes(n));
  sel.value = prev && models.includes(prev) ? prev : (pref || models[0] || "");
}

/* ---------------------------------------------------- WebSocket 指标 */

function connectMetrics() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/metrics`);
  ws.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.history) history = d.history;
      render(d.metrics);
    } catch (_) {}
  };
  ws.onclose = () => {
    setChip($("chip-pi"), "warn", "PI LINK LOST");
    setTimeout(connectMetrics, 2000);
  };
  ws.onerror = () => ws.close();
}

/* ---------------------------------------------------- Agent */

let agentWs = null;
let agentQueue = [];

function connectAgent() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  agentWs = new WebSocket(`${proto}://${location.host}/ws/agent`);
  agentWs.onopen = () => {
    $("agent-state").textContent = "IDLE";
    agentQueue.forEach((q) => agentWs.send(JSON.stringify(q)));
    agentQueue = [];
  };
  agentWs.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "log") appendLog(m.text);
    else if (m.type === "token") appendToken(m.text);
    else if (m.type === "done") finishAgent();
  };
  agentWs.onclose = () => setTimeout(connectAgent, 2500);
  agentWs.onerror = () => agentWs.close();
}

let currentBot = null;

function appendLog(text) {
  const d = document.createElement("div");
  d.className = "msg log";
  d.textContent = text;
  $("agent-log").appendChild(d);
  scrollLog();
}

function appendToken(text) {
  if (!currentBot) {
    currentBot = document.createElement("div");
    currentBot.className = "msg bot cursor";
    $("agent-log").appendChild(currentBot);
  }
  currentBot.textContent += text;
  scrollLog();
}

function scrollLog() {
  const log = $("agent-log");
  log.scrollTop = log.scrollHeight;
}

function finishAgent() {
  if (currentBot) currentBot.classList.remove("cursor");
  currentBot = null;
  agentBusy = false;
  $("agent-send").disabled = false;
  $("agent-state").textContent = "IDLE";
}

function ask(question) {
  if (agentBusy || !question.trim()) return;
  agentBusy = true;
  $("agent-send").disabled = true;
  $("agent-state").textContent = "RUNNING";
  currentBot = null;

  const u = document.createElement("div");
  u.className = "msg user";
  u.textContent = question;
  $("agent-log").appendChild(u);
  scrollLog();

  if (agentWs && agentWs.readyState === WebSocket.OPEN) {
    agentWs.send(JSON.stringify({ question, model: $("agent-model-select").value }));
  } else {
    agentQueue.push({ question, model: $("agent-model-select").value });
    appendLog("◌ 正在建立 Agent 链路…");
  }
}

/* ---------------------------------------------------- 初始化 */

const clockTime = $("clock-time");
const clockDate = $("clock-date");
function tickClock() {
  const now = new Date();
  clockTime.textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
  clockDate.textContent = now.toLocaleDateString("zh-CN") + " · CST";
}

function loop(t) {
  drawBg(t);
  if (latest) {
    drawRadar(t);
    drawTrace();
    drawSpark();
  }
  requestAnimationFrame(loop);
}

function init() {
  resizeBg();
  tickClock();
  setInterval(tickClock, 1000);
  connectMetrics();
  connectAgent();

  $("agent-send").addEventListener("click", () => {
    ask($("agent-input").value);
    $("agent-input").value = "";
  });
  $("agent-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      ask($("agent-input").value);
      $("agent-input").value = "";
    }
  });
  document.querySelectorAll(".quick button").forEach((b) => {
    b.addEventListener("click", () => ask(b.dataset.q));
  });
  addEventListener("resize", () => {
    resizeBg();
    if (latest) render(latest);
  });
  requestAnimationFrame(loop);
}

init();
