// 主入口：计时循环、UI 绑定、设置持久化、托盘联动、BLE 推送、系统通知。

import {
  DEFAULT_DURATIONS,
  Durations,
  PHASE_NAMES,
  PomodoroState,
  advance,
  clearSavedState,
  lastNDays,
  loadState,
  mmss,
  newState,
  phaseSecondsFor,
  recordPomodoro,
  saveState,
  skip,
} from "./timer.js";

declare global {
  interface Window {
    __TAURI__?: {
      core: { invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown> };
      event: {
        listen: <T>(event: string, cb: (e: { payload: T }) => void) => Promise<void>;
      };
    };
  }
}

interface PushReport {
  planes: number;
  payloadBytes: number;
  encodedBytes: number;
  packets: number;
  checksum: number;
}

interface DeviceInfo {
  address: string;
  name: string;
  rssi: number | null;
}

interface Settings {
  workMin: number;
  shortMin: number;
  longMin: number;
  rounds: number;
  pushEnabled: boolean;
  pushInterval: number; // 分钟
  driver: string;
  autostart: boolean;
}

const $ = (id: string): HTMLElement => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`缺少 #${id}`);
  return el;
};

const canvas = $("screen") as HTMLCanvasElement;

const statusEl = $("status");
const pushStatusEl = $("push-status");
const deviceEl = $("device") as HTMLSelectElement;
const logEl = $("log");
const startBtn = $("start") as HTMLButtonElement;
const resetBtn = $("reset") as HTMLButtonElement;
const skipBtn = $("skip") as HTMLButtonElement;
const pushBtn = $("push") as HTMLButtonElement;
const scanBtn = $("scan") as HTMLButtonElement;
const workEl = $("work") as HTMLInputElement;
const shortEl = $("short") as HTMLInputElement;
const longEl = $("long") as HTMLInputElement;
const roundsEl = $("rounds") as HTMLInputElement;
const pushEnabledEl = $("push-enabled") as HTMLInputElement;
const pushIntervalEl = $("push-interval") as HTMLInputElement;
const driverEl = $("driver") as HTMLSelectElement;
const autostartEl = $("autostart") as HTMLInputElement;
const statsEl = $("stats");

const ADDR_KEY = "epd42-pomodoro-address";
const SETTINGS_KEY = "epd42-pomodoro-settings";

// ── 设置持久化 ─────────────────────────────────────────────────────────────
function defaultSettings(): Settings {
  return {
    workMin: DEFAULT_DURATIONS.workMin,
    shortMin: DEFAULT_DURATIONS.shortMin,
    longMin: DEFAULT_DURATIONS.longMin,
    rounds: DEFAULT_DURATIONS.rounds,
    pushEnabled: false,
    pushInterval: 3,
    driver: "2",
    autostart: false,
  };
}

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return defaultSettings();
    return { ...defaultSettings(), ...(JSON.parse(raw) as Partial<Settings>) };
  } catch {
    return defaultSettings();
  }
}

function saveSettings(s: Settings): void {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
}

function applySettings(s: Settings): void {
  workEl.value = String(s.workMin);
  shortEl.value = String(s.shortMin);
  longEl.value = String(s.longMin);
  roundsEl.value = String(s.rounds);
  pushEnabledEl.checked = s.pushEnabled;
  pushIntervalEl.value = String(s.pushInterval);
  driverEl.value = s.driver;
  autostartEl.checked = s.autostart;
}

function syncSettings(): void {
  settings.workMin = Math.max(1, Number(workEl.value) || DEFAULT_DURATIONS.workMin);
  settings.shortMin = Math.max(1, Number(shortEl.value) || DEFAULT_DURATIONS.shortMin);
  settings.longMin = Math.max(1, Number(longEl.value) || DEFAULT_DURATIONS.longMin);
  settings.rounds = Math.max(1, Math.round(Number(roundsEl.value) || DEFAULT_DURATIONS.rounds));
  settings.pushEnabled = pushEnabledEl.checked;
  settings.pushInterval = Math.max(0, Number(pushIntervalEl.value) || 3);
  settings.driver = driverEl.value;
  saveSettings(settings);
}

function durationsOf(s: Settings): Durations {
  return { workMin: s.workMin, shortMin: s.shortMin, longMin: s.longMin, rounds: s.rounds };
}

// ── 状态 ─────────────────────────────────────────────────────────────────
let settings: Settings = loadSettings();
applySettings(settings);

let state: PomodoroState = loadState() ?? newState(durationsOf(settings));
let deadline = Date.now() / 1000 + state.remaining;
let nextPushAt = Date.now() / 1000;
let lastTooltipAt = 0;

function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (!window.__TAURI__) {
    return Promise.reject(new Error("当前运行在浏览器里（未检测到 Tauri），蓝牙功能不可用"));
  }
  return window.__TAURI__.core.invoke(cmd, args) as Promise<T>;
}

function log(message: string): void {
  const time = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  logEl.textContent = `${p(time.getHours())}:${p(time.getMinutes())}:${p(time.getSeconds())}  ${message}\n` + logEl.textContent;
}

// ── 周统计（纯 CSS 条形图）─────────────────────────────────────────────
function renderStats(): void {
  const days = lastNDays(7);
  const max = Math.max(1, ...days.map((d) => d.count));
  const total = days.reduce((sum, d) => sum + d.count, 0);
  const bars = days
    .map((d) => {
      const height = d.count === 0 ? 3 : Math.max(6, Math.round((d.count / max) * 68));
      const cls = d.isToday ? "bar-col today" : "bar-col";
      return (
        `<div class="${cls}" title="${d.date}：${d.count} 个">` +
        `<div class="bar" style="height:${height}px"></div>` +
        `<div class="day">${d.label}</div>` +
        `<div class="count">${d.count}</div>` +
        `</div>`
      );
    })
    .join("");
  statsEl.innerHTML = `<div class="stats-title">近 7 天共 ${total} 个番茄</div><div class="bars">${bars}</div>`;
}

// ── 画面 ────────────────────────────────────────────────────────────────
function remainingToText(): string {
  return `${Math.max(0, Math.ceil(state.remaining))}s`;
}

// ── 画面：由 Rust 渲染（预览与推送共用同一实现，消灭复刻漂移）─────────────
interface FaceState {
  phase: number;
  phaseSeconds: number;
  remaining: number;
  running: boolean;
  pomodoroCount: number;
  cycleTotal: number;
  rounds: number;
  stamp: string;
}

function faceStateOf(): FaceState {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  const phases = ["work", "short_break", "long_break"];
  return {
    phase: phases.indexOf(state.phase),
    phaseSeconds: state.phaseSeconds,
    remaining: Math.max(0, Math.ceil(state.remaining)),
    running: state.running,
    pomodoroCount: state.pomodoroCount,
    cycleTotal: state.cycleTotal,
    rounds: state.rounds,
    stamp: `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`,
  };
}

let lastFaceSignature = "";

function syncFace(): void {
  const face = faceStateOf();
  // 分钟级画面：内容没变就不重新渲染，避免逐帧 IPC
  const sig = `${face.phase}|${Math.floor(face.remaining / 60)}|${face.running}|${face.pomodoroCount}|${face.cycleTotal}|${face.rounds}`;
  if (sig === lastFaceSignature) return;
  lastFaceSignature = sig;
  invoke<number[]>("render_face", { state: face })
    .then((luma) => blitFace(luma))
    .catch(() => {});
}

function blitFace(luma: number[]): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const rgba = new Uint8ClampedArray(400 * 300 * 4);
  for (let i = 0; i < 400 * 300; i += 1) {
    const v = luma[i] as number;
    rgba[i * 4] = v;
    rgba[i * 4 + 1] = v;
    rgba[i * 4 + 2] = v;
    rgba[i * 4 + 3] = 255;
  }
  ctx.putImageData(new ImageData(rgba, 400, 300), 0, 0);
}

function redraw(): void {
  const [zh] = PHASE_NAMES[state.phase];
  statusEl.textContent =
    `${state.running ? "▶" : "⏸"} 剩 ${remainingToText()}  本轮 ${state.pomodoroCount}/${state.rounds}`;
  // 任务栏/标题实时显示：专注 23:41
  document.title = `${state.running ? "▶" : "⏸"} ${zh} ${mmss(state.remaining)} · EPD42 番茄钟`;
}

// ── 托盘 ────────────────────────────────────────────────────────────────
function listenTray(): void {
  if (!window.__TAURI__) return;
  const { event } = window.__TAURI__;
  void event.listen<null>("menu-toggle", () => startBtn.click());
  void event.listen<null>("menu-push", () => void doPush());
  // 全局快捷键经 Rust 分流后发 menu-push / menu-toggle（见 src-tauri/src/lib.rs）
}

function updateTrayTooltip(): void {
  if (!window.__TAURI__ || Date.now() - lastTooltipAt < 5000) return;
  lastTooltipAt = Date.now();
  const [zh] = PHASE_NAMES[state.phase];
  const text = `${state.running ? "▶" : "⏸"} ${zh} ${mmss(state.remaining)} · 今日 ${state.cycleTotal} 个`;
  invoke("set_tray_tooltip", { text }).catch(() => {
    /* 气泡更新失败不影响计时 */
  });
}

// ── 推送 ────────────────────────────────────────────────────────────────
function currentAddress(): string | null {
  const addr = deviceEl.value;
  if (addr && addr !== "__none__") return addr;
  return localStorage.getItem(ADDR_KEY);
}

const PUSH_MAX_ATTEMPTS = 3;
const PUSH_RETRY_DELAY_MS = 1500;

async function doPush(): Promise<boolean> {
  pushBtn.disabled = true;
  try {
    for (let attempt = 1; attempt <= PUSH_MAX_ATTEMPTS; attempt += 1) {
      try {
        const report = await invoke<PushReport>("push_frame", {
          state: faceStateOf(),
          driver: Number(settings.driver),
          address: currentAddress(),
        });
        pushStatusEl.textContent =
          `✓ ${report.planes} 平面 ${report.payloadBytes}B → 编码 ${report.encodedBytes}B / ${report.packets} 包`;
        log(`推送成功：${report.planes} 平面，${report.encodedBytes} 字节 / ${report.packets} 包`);
        if (deviceEl.value && deviceEl.value !== "__none__") {
          localStorage.setItem(ADDR_KEY, deviceEl.value);
        }
        return true;
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        if (attempt < PUSH_MAX_ATTEMPTS) {
          pushStatusEl.textContent = `↻ 第 ${attempt} 次失败，${PUSH_RETRY_DELAY_MS / 1000}s 后重试：${message}`;
          log(`推送失败（第 ${attempt} 次），稍后重试：${message}`);
          await new Promise((resolve) => setTimeout(resolve, PUSH_RETRY_DELAY_MS));
        } else {
          pushStatusEl.textContent = `✗ 多次失败，设备可能离线：${message}`;
          log(`推送失败（重试 ${PUSH_MAX_ATTEMPTS - 1} 次后放弃）：${message}`);
          return false;
        }
      }
    }
    return false;
  } finally {
    pushBtn.disabled = false;
  }
}

async function scanDevices(): Promise<void> {
  scanBtn.disabled = true;
  deviceEl.innerHTML = '<option value="__none__">（自动查找 NRF_EPD）</option>';
  try {
    const devices = await invoke<DeviceInfo[]>("scan_devices", { timeoutSecs: 4 });
    const saved = localStorage.getItem(ADDR_KEY);
    for (const d of devices) {
      const name = d.name || "(未命名)";
      const opt = document.createElement("option");
      opt.value = d.address;
      opt.textContent = `${name}  ${d.address}${d.rssi !== null ? `  ${d.rssi}dB` : ""}`;
      deviceEl.appendChild(opt);
      if (d.address === saved) deviceEl.value = d.address;
    }
    log(`扫描到 ${devices.length} 个设备；EPD42 广播名是 NRF_EPD_*`);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    log(`扫描失败：${message}`);
  } finally {
    scanBtn.disabled = false;
  }
}

// ── 通知 / 提示音 ──────────────────────────────────────────────────────
function beep(): void {
  try {
    const AudioCtx = window.AudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    osc.frequency.value = 880;
    osc.type = "sine";
    const gain = ctx.createGain();
    gain.gain.value = 0.15;
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.5);
  } catch {
    /* 无音频设备时忽略 */
  }
}

function notifyPhase(title: string, body: string): void {
  beep();
  invoke("notify", { title, body }).catch(() => {
    /* 通知失败不阻塞计时 */
  });
}

// ── 计时主循环 ──────────────────────────────────────────────────────────
function tick(): void {
  if (state.running) {
    state.remaining = Math.max(0, Math.ceil(deadline - Date.now() / 1000));
    saveState(state);

    if (state.remaining <= 0) {
      const previous = state.phase;
      advance(state, durationsOf(settings));
      if (previous === "work") {
        recordPomodoro();
        renderStats();
      }
      state.running = true;
      deadline = Date.now() / 1000 + state.remaining;
      log(`阶段切换：${previous} → ${state.phase}`);
      notifyPhase(
        "番茄钟",
        `${previous === "work" ? "专注结束" : "休息结束"}，开始${state.phase === "work" ? "专注" : "休息"}（${Math.ceil(state.remaining / 60)} 分钟）`,
      );
      if (settings.pushEnabled) void doPush();
    }
  }

  // 周期推送（默认每 3 分钟）
  const now = Date.now() / 1000;
  if (state.running && settings.pushEnabled) {
    const interval = Math.max(0, settings.pushInterval * 60 || 180);
    if (interval > 0 && now >= nextPushAt) {
      void doPush();
      nextPushAt = now + interval;
    }
  }
  updateTrayTooltip();
  redraw();
  syncFace();
}

// ── 控件 ────────────────────────────────────────────────────────────────
startBtn.addEventListener("click", () => {
  if (state.running) {
    state.remaining = Math.max(1, Math.ceil(deadline - Date.now() / 1000));
    state.running = false;
    startBtn.textContent = "开始";
  } else {
    state.running = true;
    deadline = Date.now() / 1000 + state.remaining;
    startBtn.textContent = "暂停";
  }
  saveState(state);
});

resetBtn.addEventListener("click", () => {
  state.remaining = phaseSecondsFor(state.phase, durationsOf(settings));
  state.running = false;
  deadline = Date.now() / 1000 + state.remaining;
  startBtn.textContent = "开始";
  saveState(state);
  log("已重置当前阶段");
});

skipBtn.addEventListener("click", () => {
  const previous = state.phase;
  skip(state, durationsOf(settings));
  state.running = true;
  deadline = Date.now() / 1000 + state.remaining;
  saveState(state);
  log(`已跳过：${previous} → ${state.phase}`);
});

pushBtn.addEventListener("click", () => void doPush());
scanBtn.addEventListener("click", () => void scanDevices());

["work", "short", "long", "rounds"].forEach((id) =>
  $(id).addEventListener("change", () => {
    syncSettings();
    log(`时长已更新：${settings.workMin}/${settings.shortMin}/${settings.longMin} 分钟，每 ${settings.rounds} 个长休息`);
    redraw();
  }),
);

pushEnabledEl.addEventListener("change", () => {
  syncSettings();
  log(`自动推送已${settings.pushEnabled ? "开启" : "关闭"}`);
});
pushIntervalEl.addEventListener("change", syncSettings);
driverEl.addEventListener("change", syncSettings);

autostartEl.addEventListener("change", () => {
  syncSettings();
  invoke<boolean>("set_autostart", { enabled: autostartEl.checked })
    .then((actual) => {
      autostartEl.checked = actual;
      settings.autostart = actual;
      saveSettings(settings);
      log(`开机自启${actual ? "已开启" : "已关闭"}`);
    })
    .catch((err) => {
      autostartEl.checked = settings.autostart;
      const message = err instanceof Error ? err.message : String(err);
      log(`设置开机自启失败：${message}`);
    });
});

document.getElementById("wipe")?.addEventListener("click", () => {
  clearSavedState();
  state = newState(durationsOf(settings));
  state.running = false;
  deadline = Date.now() / 1000 + state.remaining;
  log("已清除计时状态");
  redraw();
});

// ── 启动 ────────────────────────────────────────────────────────────────
function start(): void {
  syncSettings();
  if (state.running) startBtn.textContent = "暂停";
  redraw();
  renderStats();
  listenTray();
  void scanDevices();
  // 回读开机自启的真实状态（避免覆盖系统设置）
  invoke<boolean>("get_autostart")
    .then((on) => {
      autostartEl.checked = on;
      settings.autostart = on;
      saveSettings(settings);
    })
    .catch(() => {});
  window.addEventListener("beforeunload", () => saveState(state));
  setInterval(tick, 250);
}

start();