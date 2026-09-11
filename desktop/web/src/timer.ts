// Display types and one-time legacy import only. Rust owns all timer mutations.
export type Phase = "work" | "short_break" | "long_break";
export const PHASE_NAMES: Record<Phase, string> = { work: "专注", short_break: "短休息", long_break: "长休息" };
export interface Session { id: number; task: string; endedAt: number; seconds: number; outcome: "completed" | "skipped" | "interrupted" }
export interface TimerState {
  phase: Phase; phaseSeconds: number; remaining: number; running: boolean;
  pomodoroCount: number; cycleTotal: number; cycleDate: string; rounds: number;
  updatedAt: number; task: string; nextTask: string | null; sessions: Session[];
  sessionId: number; expiredAt: number | null;
}
export interface Settings {
  workMin: number; shortMin: number; longMin: number; rounds: number;
  autoAdvance: boolean; pushEnabled: boolean; pushInterval: number;
  scanTimeout: number; driver: string; address: string | null;
}
export interface Snapshot { version: number; revision: number; state: TimerState; settings: Settings; stats: Record<string, number>; message: string }
export interface Migration { state: TimerState | null; settings: Settings; stats: Record<string, number> }
export const DEFAULT_SETTINGS: Settings = { workMin: 25, shortMin: 5, longMin: 15, rounds: 4,
  autoAdvance: true, pushEnabled: false, pushInterval: 3, scanTimeout: 10, driver: "2", address: null };
export function boundedNumber(value: unknown, fallback: number, min: number, max: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
}
export function mmss(seconds: number): string {
  const n = Math.max(0, Math.ceil(seconds));
  return `${String(Math.floor(n / 60)).padStart(2, "0")}:${String(n % 60).padStart(2, "0")}`;
}
function object(raw: string | null): Record<string, unknown> {
  if (!raw) return {};
  const parsed: unknown = JSON.parse(raw);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
}
export function normalizeSettings(raw: Record<string, unknown>): Settings {
  return {
    workMin: boundedNumber(raw.workMin, 25, 1, 1440), shortMin: boundedNumber(raw.shortMin, 5, 1, 1440),
    longMin: boundedNumber(raw.longMin, 15, 1, 1440), rounds: Math.round(boundedNumber(raw.rounds, 4, 1, 12)),
    autoAdvance: raw.autoAdvance !== false, pushEnabled: raw.pushEnabled === true,
    pushInterval: boundedNumber(raw.pushInterval, 3, 0, 1440), scanTimeout: Math.round(boundedNumber(raw.scanTimeout, 10, 3, 60)),
    driver: ["1", "2", "3"].includes(String(raw.driver)) ? String(raw.driver) : "2",
    address: typeof raw.address === "string" && raw.address.trim() ? raw.address.trim() : null,
  };
}
export function legacyMigration(storage: Pick<Storage, "getItem">): Migration {
  const settings = normalizeSettings({ ...object(storage.getItem("epd42-pomodoro-settings")), address: storage.getItem("epd42-pomodoro-address") });
  const data = object(storage.getItem("epd42-pomodoro-state"));
  const stats: Record<string, number> = {};
  for (const [date, count] of Object.entries(object(storage.getItem("epd42-pomodoro-stats")))) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(date) && typeof count === "number" && Number.isFinite(count) && count >= 0)
      stats[date] = Math.trunc(Math.min(count, 1000000));
  }
  if (!Object.keys(data).length) return { state: null, settings, stats };
  if (!["work", "short_break", "long_break"].includes(String(data.phase))) throw new Error("旧版计时阶段无效，原数据已保留");
  const sessions: Session[] = [];
  if (Array.isArray(data.sessions)) for (const item of data.sessions) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    if (typeof row.task === "string" && typeof row.endedAt === "number" && Number.isFinite(row.endedAt)
      && typeof row.seconds === "number" && Number.isFinite(row.seconds)
      && ["completed", "skipped", "interrupted"].includes(String(row.outcome))) {
      sessions.push({ id: sessions.length + 1, task: row.task, endedAt: Math.trunc(row.endedAt), seconds: Math.max(0, row.seconds), outcome: row.outcome as Session["outcome"] });
    }
  }
  const phaseSeconds = boundedNumber(data.phaseSeconds, 1500, 1, 86400);
  return { settings, stats, state: {
    phase: data.phase as Phase, phaseSeconds, remaining: boundedNumber(data.remaining, phaseSeconds, 0, phaseSeconds),
    running: data.running === true, pomodoroCount: Math.trunc(boundedNumber(data.pomodoroCount, 0, 0, 1000000)),
    cycleTotal: Math.trunc(boundedNumber(data.cycleTotal, 0, 0, 1000000)), cycleDate: typeof data.cycleDate === "string" ? data.cycleDate : "",
    rounds: settings.rounds, updatedAt: boundedNumber(data.updatedAt, 0, 0, Number.MAX_SAFE_INTEGER),
    task: typeof data.task === "string" ? data.task.slice(0, 120) : "", nextTask: null,
    sessions: sessions.slice(-500), sessionId: sessions.length + 1,
    expiredAt: typeof data.expiredAt === "number" && Number.isFinite(data.expiredAt) ? Math.trunc(data.expiredAt) : null,
  } };
}
