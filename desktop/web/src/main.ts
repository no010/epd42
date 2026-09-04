// 主入口：计时循环、UI 绑定、设置持久化、托盘联动、BLE 推送、系统通知。

import { drawFace, lumaBytes } from "./face.js";
import {
  DEFAULT_DURATIONS,
  Durations,
  PHASE_NAMES,
  PomodoroState,
  advance,
  clearSavedState,
  loadState,
  mmss,
  newState,
  phaseSecondsFor,
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

// ── 画面 ────────────────────────────────────────────────────────────────
function remainingToText(): string {
  return `${Math.max(0, Math.ceil(state.remaining))}s`;
}

function redraw(): void {
  drawFace(canvas, state);
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

async function doPush(): Promise<boolean> {
  pushBtn.disabled = true;
  try {
    const report = await invoke<PushReport>("push_frame", {
      pixels: Array.from(lumaBytes(canvas)),
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
    pushStatusEl.textContent = `✗ ${message}`;
    log(`推送失败：${message}`);
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