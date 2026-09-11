import { DEFAULT_SETTINGS, PHASE_NAMES, Snapshot, Settings, legacyMigration, mmss, normalizeSettings } from "./timer.js";

declare global { interface Window { __TAURI__?: {
  core: { invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown> };
  event: { listen: <T>(event: string, cb: (e: { payload: T }) => void) => Promise<() => void> };
} } }
const $ = (id: string): HTMLElement => { const el = document.getElementById(id); if (!el) throw new Error(`缺少 #${id}`); return el; };
const input = (id: string) => $(id) as HTMLInputElement;
const device = $("device") as HTMLSelectElement;
const canvas = $("screen") as HTMLCanvasElement;
const controls = [...document.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>("input,button,select")];
let snapshot: Snapshot | null = null;
let ready = false;
let commandChain: Promise<void> = Promise.resolve();
let lastFace = "";
let lastHistory = "";
let lastStats = "";
let lastMessage = "";
let lastPush = "";
function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (!window.__TAURI__) return Promise.reject(new Error("请使用桌面应用运行"));
  return window.__TAURI__.core.invoke(cmd, args) as Promise<T>;
}
function log(message: string): void {
  $("log").textContent = (`${new Date().toLocaleTimeString()}  ${message}\n` + $("log").textContent).slice(0, 12000);
}
function failure(error: unknown): void { log(String(error)); }
function queue(work: () => Promise<void>): void {
  if (!ready) return;
  commandChain = commandChain.then(work).catch(failure);
}
async function action(kind: string, args: Record<string, unknown> = {}): Promise<void> {
  accept(await invoke<Snapshot>("timer_action", { kind, ...args }));
}
function readSettings(): Settings {
  return normalizeSettings({ workMin: Number(input("work").value), shortMin: Number(input("short").value),
    longMin: Number(input("long").value), rounds: Number(input("rounds").value), scanTimeout: Number(input("scan-timeout").value),
    pushInterval: Number(input("push-interval").value), driver: input("driver").value,
    autoAdvance: input("auto-advance").checked, pushEnabled: input("push-enabled").checked,
    address: device.value === "__none__" ? null : device.value });
}
function applySettings(settings: Settings): void {
  const values: Record<string, string | number> = { work: settings.workMin, short: settings.shortMin, long: settings.longMin,
    rounds: settings.rounds, "scan-timeout": settings.scanTimeout, "push-interval": settings.pushInterval, driver: settings.driver };
  for (const [id, value] of Object.entries(values)) if (document.activeElement !== $(id)) input(id).value = String(value);
  input("auto-advance").checked = settings.autoAdvance;
  input("push-enabled").checked = settings.pushEnabled;
  if (settings.address && !Array.from(device.options).some((opt) => opt.value === settings.address))
    device.appendChild(new Option(`已绑定 ${settings.address}`, settings.address));
  device.value = settings.address ?? "__none__";
}
function accept(next: Snapshot): void {
  if (snapshot && next.revision < snapshot.revision) return;
  snapshot = next;
  if (!ready) return;
  const state = next.state;
  $("start").textContent = state.running ? "暂停" : "开始";
  $("status").textContent = `${state.running ? "▶" : "⏸"} ${PHASE_NAMES[state.phase]} ${mmss(state.remaining)} · 本轮 ${state.pomodoroCount}/${state.rounds} · 今日 ${state.cycleTotal} 个`;
  document.title = `${PHASE_NAMES[state.phase]} ${mmss(state.remaining)} · EPD42 番茄钟`;
  input("task").disabled = state.running || state.remaining < state.phaseSeconds;
  if (document.activeElement !== input("task")) input("task").value = state.task;
  if (document.activeElement !== input("next-task")) input("next-task").value = state.nextTask ?? "";
  // Settings are applied on initialization and command responses, not on every
  // clock event: a delayed event must not overwrite an in-progress user edit.
  if (next.message && next.message !== lastMessage) log(next.message);
  lastMessage = next.message;
  const history = JSON.stringify(state.sessions);
  if (history !== lastHistory) {
    lastHistory = history;
    $("history").replaceChildren();
    const labels = { completed: "完成", skipped: "跳过", interrupted: "中断" };
    for (const row of state.sessions.slice(-20).reverse()) {
      const item = document.createElement("div");
      item.textContent = `${new Date(row.endedAt).toLocaleString()} · ${row.task || "未命名任务"} · ${labels[row.outcome]} · ${(row.seconds / 60).toFixed(1)} 分钟`;
      $("history").appendChild(item);
    }
    if (!state.sessions.length) $("history").textContent = "还没有专注记录。";
  }
  const stats = JSON.stringify([next.stats, state.cycleDate]);
  if (stats !== lastStats) { lastStats = stats; renderStats(next.stats); }
  const sig = JSON.stringify([state.phase, Math.ceil(state.remaining / 60), state.phaseSeconds, state.running,
    state.pomodoroCount, state.cycleTotal, state.rounds, Math.floor(Date.now() / 60000)]);
  if (sig !== lastFace) {
    lastFace = sig;
    void invoke<number[]>("render_face").then((luma) => {
      if (lastFace !== sig) return;
      const rgba = new Uint8ClampedArray(400 * 300 * 4);
      luma.forEach((v, i) => { rgba[i * 4] = v; rgba[i * 4 + 1] = v; rgba[i * 4 + 2] = v; rgba[i * 4 + 3] = 255; });
      canvas.getContext("2d")?.putImageData(new ImageData(rgba, 400, 300), 0, 0);
    }).catch((e: unknown) => { if (lastFace === sig) lastFace = ""; failure(e); });
  }
}
function renderStats(stats: Record<string, number>): void {
  const days = Array.from({ length: 7 }, (_, i) => {
    const day = new Date(); day.setDate(day.getDate() - (6 - i));
    const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    return { key, count: stats[key] ?? 0, label: i === 6 ? "今" : "日一二三四五六"[day.getDay()] };
  });
  const max = Math.max(1, ...days.map((d) => d.count));
  $("stats").innerHTML = `<div class="stats-title">近 7 天共 ${days.reduce((sum, d) => sum + d.count, 0)} 个番茄</div><div class="bars">` +
    days.map((d) => `<div class="bar-col"><div class="bar" style="height:${Math.max(3, Math.round(d.count / max * 68))}px"></div><div class="day">${d.label}</div><div class="count">${d.count}</div></div>`).join("") + "</div>";
}
interface PushStatus { busy: boolean; message: string; lastSuccess: number | null }
function pushStatus(status: PushStatus): void {
  $("push-status").textContent = status.message + (status.lastSuccess ? ` · 上次成功 ${new Date(status.lastSuccess).toLocaleTimeString()}` : "");
  ($( "cancel-push") as HTMLButtonElement).disabled = !status.busy;
  if (status.message && status.message !== lastPush) log(status.message);
  lastPush = status.message;
}
async function scan(): Promise<void> {
  const button = $("scan") as HTMLButtonElement;
  button.disabled = true;
  try {
    const devices = await invoke<{ address: string; name: string; rssi: number | null }[]>("scan_devices", { timeoutSecs: Number(input("scan-timeout").value) });
    const selected = device.value;
    device.replaceChildren(new Option("（自动查找 NRF_EPD）", "__none__"));
    for (const d of devices) device.appendChild(new Option(`${d.name || "未命名"} ${d.address}`, d.address));
    if (selected !== "__none__" && !devices.some((d) => d.address === selected)) device.appendChild(new Option(`已绑定 ${selected}`, selected));
    device.value = selected;
    log(`扫描到 ${devices.length} 个设备`);
  } catch (e) { failure(e); } finally { button.disabled = false; }
}
for (const [id, kind] of [["start", "toggle"], ["reset", "reset"], ["skip", "skip"], ["wipe", "wipe"]])
  $(id).addEventListener("click", () => queue(() => action(kind)));
input("task").addEventListener("change", () => { const task = input("task").value; queue(() => action("task", { task })); });
$("save-next-task").addEventListener("click", () => { const task = input("next-task").value; queue(async () => { await action("next_task", { task }); log("下一番茄任务已保存"); }); });
$("clear-next-task").addEventListener("click", () => queue(async () => { await action("next_task", { task: null }); input("next-task").value = ""; }));
for (const id of ["work", "short", "long", "rounds", "scan-timeout", "push-interval", "driver", "auto-advance", "push-enabled", "device"])
  $(id).addEventListener("change", () => { const settings = readSettings(); queue(async () => { await action("settings", { settings }); if (snapshot) applySettings(snapshot.settings); }); });
$("push").addEventListener("click", () => queue(async () => { await invoke("request_push"); }));
$("cancel-push").addEventListener("click", () => { void invoke("cancel_push").catch(failure); });
$("scan").addEventListener("click", () => { if (ready) void scan(); });
input("autostart").addEventListener("change", () => {
  const enabled = input("autostart").checked;
  queue(async () => { try { input("autostart").checked = await invoke<boolean>("set_autostart", { enabled }); }
    catch (e) { input("autostart").checked = !enabled; throw e; } });
});
async function start(): Promise<void> {
  controls.forEach((c) => { c.disabled = true; });
  device.appendChild(new Option("（自动查找 NRF_EPD）", "__none__"));
  if (!window.__TAURI__) throw new Error("请使用桌面应用运行");
  await window.__TAURI__.event.listen<Snapshot>("timer-state", (e) => accept(e.payload));
  await window.__TAURI__.event.listen<string>("runtime-error", (e) => failure(e.payload));
  await window.__TAURI__.event.listen<PushStatus>("push-status", (e) => pushStatus(e.payload));
  // The backend ignores the import after its first successful atomic save.
  const existing = await invoke<Snapshot>("get_snapshot");
  const migration = existing.revision > 0 ? { state: null, settings: DEFAULT_SETTINGS, stats: {} } : legacyMigration(localStorage);
  const initial = await invoke<Snapshot>("initialize_timer", { migration });
  ready = true;
  controls.forEach((c) => { c.disabled = false; });
  applySettings(initial.settings);
  accept(initial);
  pushStatus(await invoke<PushStatus>("get_push_status"));
  input("autostart").checked = await invoke<boolean>("get_autostart");
}
void start().catch((e: unknown) => { $("status").textContent = "初始化失败，请查看下方日志"; failure(e); });
