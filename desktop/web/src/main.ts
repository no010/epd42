// 主入口：计时循环、UI 绑定、BLE 推送、系统通知。

import { drawFace, lumaBytes } from "./face.js";
import {
  DEFAULT_DURATIONS,
  PomodoroState,
  Durations,
  advance,
  clearSavedState,
  loadState,
  newState,
  phaseSecondsFor,
  saveState,
  skip,
} from "./timer.js";

declare global {
  interface Window {
    __TAURI__?: {
      core: { invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown> };
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

const $ = (id: string): HTMLElement => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`缺少 #${id}`);
  return el;
};

const canvas = $("screen") as HTMLCanvasElement;

// ── 元素 ────────────────────────────────────────────────────────────────
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

// ── 状态 ────────────────────────────────────────────────────────────────
const ADDR_KEY = "epd42-pomodoro-address";

function readDurations(): Durations {
  return {
    workMin: Math.max(1, Number(workEl.value) || DEFAULT_DURATIONS.workMin),
    shortMin: Math.max(1, Number(shortEl.value) || DEFAULT_DURATIONS.shortMin),
    longMin: Math.max(1, Number(longEl.value) || DEFAULT_DURATIONS.longMin),
    rounds: Math.max(1, Math.round(Number(roundsEl.value) || DEFAULT_DURATIONS.rounds)),
  };
}

let state: PomodoroState = loadState() ?? newState(readDurations());
let deadline = Date.now() / 1000 + state.remaining;
let nextPushAt = Date.now() / 1000; // 立刻可推（首个倒计时内只推一次）
let notifiedThisPhase = false;

const settings: Durations = readDurations();

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
function redraw(): void {
  drawFace(canvas, state);
  statusEl.textContent =
    `${state.running ? "▶" : "⏸"} 剩 ${remainingToText(state)}  本轮 ${state.pomodoroCount}/${state.rounds}`;
}

function remainingToText(state: PomodoroState): string {
  return `${Math.max(0, Math.ceil(state.remaining))}s`;
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
      driver: Number(driverEl.value),
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
      advance(state, settings);
      state.running = true;
      deadline = Date.now() / 1000 + state.remaining;
      notifiedThisPhase = false;
      log(`阶段切换：${previous} → ${state.phase}`);
      notifyPhase(
        "番茄钟",
        `${previous === "work" ? "专注结束" : "休息结束"}，开始${state.phase === "work" ? "专注" : "休息"}（${Math.ceil(state.remaining / 60)} 分钟）`,
      );
      if (pushEnabledEl.checked) void doPush();
    }
  }

  // 周期推送（默认每 3 分钟）
  const now = Date.now() / 1000;
  if (state.running && pushEnabledEl.checked) {
    const interval = Math.max(0, Number(pushIntervalEl.value) * 60 || 180);
    if (interval > 0 && now >= nextPushAt) {
      void doPush();
      nextPushAt = now + interval;
    }
  }
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
  state.remaining = phaseSecondsFor(state.phase, settings);
  state.running = false;
  deadline = Date.now() / 1000 + state.remaining;
  startBtn.textContent = "开始";
  saveState(state);
  log("已重置当前阶段");
});

skipBtn.addEventListener("click", () => {
  const previous = state.phase;
  skip(state, settings);
  state.running = true;
  deadline = Date.now() / 1000 + state.remaining;
  saveState(state);
  log(`已跳过：${previous} → ${state.phase}`);
});

pushBtn.addEventListener("click", () => void doPush());
scanBtn.addEventListener("click", () => void scanDevices());

["work", "short", "long", "rounds"].forEach((id) => $(id).addEventListener("change", () => {
  const dur = readDurations();
  settings.workMin = dur.workMin;
  settings.shortMin = dur.shortMin;
  settings.longMin = dur.longMin;
  settings.rounds = dur.rounds;
  log(`时长已更新：${dur.workMin}/${dur.shortMin}/${dur.longMin} 分钟，每 ${dur.rounds} 个长休息`);
  redraw();
}));

document.getElementById("wipe")?.addEventListener("click", () => {
  clearSavedState();
  state = newState(readDurations());
  state.running = false;
  deadline = Date.now() / 1000 + state.remaining;
  log("已清除计时状态");
  redraw();
});

// ── 启动 ────────────────────────────────────────────────────────────────
function start(): void {
  const dur = readDurations();
  settings.workMin = dur.workMin;
  settings.shortMin = dur.shortMin;
  settings.longMin = dur.longMin;
  settings.rounds = dur.rounds;
  if (!state.running) {
    startBtn.textContent = "开始";
  } else {
    startBtn.textContent = "暂停";
  }
  redraw();
  void scanDevices();
  setInterval(tick, 250);
}

start();