import { legacyMigration, normalizeSettings, mmss } from "../src/timer.js";
function check(value: boolean, message: string): void { if (!value) throw new Error(message); }
const old = new Map<string, string>([
  ["epd42-pomodoro-settings", JSON.stringify({ pushInterval: 0, autoAdvance: false })],
  ["epd42-pomodoro-address", "AA:BB:CC:DD:EE:FF"],
  ["epd42-pomodoro-state", JSON.stringify({ phase: "work", remaining: 50, phaseSeconds: 60, running: true,
    updatedAt: 12345, task: "当前任务", sessions: [{ task: "昨天任务", endedAt: 123, seconds: 60, outcome: "completed" }] })],
  ["epd42-pomodoro-stats", JSON.stringify({ "2026-09-07": 3 })],
]);
const migration = legacyMigration({ getItem: (key) => old.get(key) ?? null });
check(migration.settings.pushInterval === 0 && !migration.settings.autoAdvance, "zero interval and manual mode survive migration");
check(migration.state?.remaining === 50 && migration.state.updatedAt === 12345, "import must not subtract offline elapsed twice");
check(migration.state?.task === "当前任务" && migration.state.sessions[0].task === "昨天任务", "tasks survive migration");
check(migration.stats["2026-09-07"] === 3, "daily stats survive migration");
check(migration.settings.address === "AA:BB:CC:DD:EE:FF", "device binding survives migration");
check(normalizeSettings({ workMin: Infinity }).workMin === 25, "invalid duration rejected");
check(mmss(60.1) === "01:01", "countdown rounds up");
console.log("7 migration and display regression checks passed");
