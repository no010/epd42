// 番茄钟状态机 + localStorage 持久化（移植自 tools/epd-pomodoro/state.py）。

export type Phase = "work" | "short_break" | "long_break";

export const PHASES: Phase[] = ["work", "short_break", "long_break"];

export const PHASE_NAMES: Record<Phase, [string, string]> = {
  work: ["专注", "FOCUS"],
  short_break: ["短休息", "SHORT BREAK"],
  long_break: ["长休息", "LONG BREAK"],
};

export interface Durations {
  workMin: number;
  shortMin: number;
  longMin: number;
  rounds: number;
}

export const DEFAULT_DURATIONS: Durations = { workMin: 25, shortMin: 5, longMin: 15, rounds: 4 };

// 圆点要画进 400px 宽度：轮次上限防止圆点溢出画面
export const MAX_ROUNDS = 12;

export interface PomodoroState {
  phase: Phase;
  phaseSeconds: number;
  remaining: number;
  running: boolean;
  pomodoroCount: number;
  cycleTotal: number;
  cycleDate: string;
  rounds: number;
  updatedAt: number; // epoch 秒
}

const STORAGE_KEY = "epd42-pomodoro-state";

export function minutesToSeconds(minutes: number): number {
  return Math.max(1, Math.round(minutes * 60));
}

export function mmss(seconds: number): string {
  const total = Math.max(0, Math.ceil(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function toKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function today(): string {
  return toKey(new Date());
}

export function newState(dur: Durations): PomodoroState {
  const phaseSeconds = minutesToSeconds(dur.workMin);
  return {
    phase: "work",
    phaseSeconds,
    remaining: phaseSeconds,
    running: false,
    pomodoroCount: 0,
    cycleTotal: 0,
    cycleDate: today(),
    rounds: Math.min(MAX_ROUNDS, Math.max(1, Math.round(dur.rounds))),
    updatedAt: Date.now() / 1000,
  };
}

export function saveState(state: PomodoroState): void {
  state.updatedAt = Date.now() / 1000;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

export function clearSavedState(): void {
  localStorage.removeItem(STORAGE_KEY);
}

export function loadState(): PomodoroState | null {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const data = JSON.parse(raw) as Partial<PomodoroState>;
    if (!data.phase || !PHASES.includes(data.phase as Phase)) return null;
    const state: PomodoroState = {
      phase: data.phase as Phase,
      phaseSeconds: Math.max(1, Math.trunc(data.phaseSeconds ?? 1500)),
      remaining: Math.max(0, Math.min(Math.trunc(data.remaining ?? 0), data.phaseSeconds ?? 1500)),
      running: Boolean(data.running),
      pomodoroCount: Math.trunc(data.pomodoroCount ?? 0),
      cycleTotal: Math.trunc(data.cycleTotal ?? 0),
      cycleDate: typeof data.cycleDate === "string" ? data.cycleDate : "",
      rounds: Math.min(MAX_ROUNDS, Math.max(1, Math.trunc(data.rounds ?? 4))),
      updatedAt: typeof data.updatedAt === "number" ? data.updatedAt : 0,
    };
    // 加载时若在运行中，按墙钟回拨
    if (state.running && state.updatedAt > 0) {
      const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - state.updatedAt));
      state.remaining = Math.max(0, state.remaining - elapsed);
      if (state.remaining === 0) state.running = false;
    }
    return state;
  } catch {
    return null;
  }
}

const DURATION_KEYS: Record<Phase, keyof Durations> = {
  work: "workMin",
  short_break: "shortMin",
  long_break: "longMin",
};

export function phaseSecondsFor(phase: Phase, dur: Durations): number {
  return minutesToSeconds(dur[DURATION_KEYS[phase]]);
}

/** 当前阶段结束：番茄计数 +1（跨天归零），进入短/长休息或回到专注。 */
export function advance(state: PomodoroState, dur: Durations): void {
  const todayStr = today();
  if (state.cycleDate !== todayStr) {
    state.cycleDate = todayStr;
    state.cycleTotal = 0;
  }
  if (state.phase === "work") {
    state.pomodoroCount += 1;
    state.cycleTotal += 1;
    state.phase = state.pomodoroCount % state.rounds === 0 ? "long_break" : "short_break";
  } else {
    if (state.phase === "long_break") state.pomodoroCount = 0;
    state.phase = "work";
  }
  state.phaseSeconds = phaseSecondsFor(state.phase, dur);
  state.remaining = state.phaseSeconds;
}

/** 手动跳过：不计数。 */
export function skip(state: PomodoroState, dur: Durations): void {
  if (state.phase === "work") {
    state.phase = "short_break";
  } else {
    if (state.phase === "long_break") state.pomodoroCount = 0;
    state.phase = "work";
  }
  state.phaseSeconds = phaseSecondsFor(state.phase, dur);
  state.remaining = state.phaseSeconds;
}
// ── 每日番茄统计（localStorage，只留最近 14 天）───────────────────────────
export type DailyStats = Record<string, number>;

const STATS_KEY = "epd42-pomodoro-stats";

export function loadStats(): DailyStats {
  try {
    const raw = localStorage.getItem(STATS_KEY);
    const stats: DailyStats = raw ? (JSON.parse(raw) as DailyStats) : {};
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - 13);
    const kept: DailyStats = {};
    for (const [date, count] of Object.entries(stats)) {
      if (date >= toKey(cutoff) && count > 0) kept[date] = count;
    }
    return kept;
  } catch {
    return {};
  }
}

/** 完成一个专注时调用，当天计数 +1。 */
export function recordPomodoro(): void {
  const key = today();
  const stats = loadStats();
  stats[key] = (stats[key] ?? 0) + 1;
  localStorage.setItem(STATS_KEY, JSON.stringify(stats));
}

export interface DayStat {
  date: string;
  label: string;
  count: number;
  isToday: boolean;
}

/** 最近 n 天（含今天），按时间升序。 */
export function lastNDays(n: number): DayStat[] {
  const stats = loadStats();
  const todayKey = today();
  const weekdays = ["日", "一", "二", "三", "四", "五", "六"];
  const out: DayStat[] = [];
  for (let i = n - 1; i >= 0; i -= 1) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    const key = toKey(d);
    out.push({
      date: key,
      label: key === todayKey ? "今" : weekdays[d.getDay()],
      count: stats[key] ?? 0,
      isToday: key === todayKey,
    });
  }
  return out;
}