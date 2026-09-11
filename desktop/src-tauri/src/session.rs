//! Authoritative timer and one atomic document for state, records and statistics.
use std::{collections::BTreeMap, io::Write, path::PathBuf, time::{Duration, Instant}};
use chrono::{Local, TimeZone};
use serde::{Deserialize, Serialize};

pub fn now_ms() -> i64 { Local::now().timestamp_millis() }
pub fn date(ms: i64) -> String {
    Local.timestamp_millis_opt(ms).single().unwrap_or_else(Local::now).format("%Y-%m-%d").to_string()
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Settings {
    pub work_min: f64, pub short_min: f64, pub long_min: f64, pub rounds: u32,
    pub auto_advance: bool, pub push_enabled: bool, pub push_interval: f64,
    pub scan_timeout: u64, pub driver: String, pub address: Option<String>,
}
impl Default for Settings {
    fn default() -> Self { Self { work_min: 25., short_min: 5., long_min: 15., rounds: 4,
        auto_advance: true, push_enabled: false, push_interval: 3., scan_timeout: 10,
        driver: "2".into(), address: None } }
}
impl Settings {
    pub fn validate(&self) -> Result<(), String> {
        if ![self.work_min, self.short_min, self.long_min].iter().all(|n| n.is_finite() && (1.0..=1440.0).contains(n))
            || !(1..=12).contains(&self.rounds) || !self.push_interval.is_finite()
            || !(0.0..=1440.0).contains(&self.push_interval) || !(3..=60).contains(&self.scan_timeout)
            || !["1", "2", "3"].contains(&self.driver.as_str()) { return Err("设置数值超出允许范围".into()); }
        Ok(())
    }
    fn seconds(&self, phase: &str) -> f64 {
        (match phase { "short_break" => self.short_min, "long_break" => self.long_min, _ => self.work_min } * 60.).round()
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Record {
    #[serde(default)] pub id: u64,
    pub task: String, pub ended_at: i64, pub seconds: f64, pub outcome: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct TimerState {
    pub phase: String, pub phase_seconds: f64, pub remaining: f64, pub running: bool,
    pub pomodoro_count: u32, pub cycle_total: u32, pub cycle_date: String, pub rounds: u32,
    pub updated_at: f64, pub task: String, pub next_task: Option<String>, pub sessions: Vec<Record>,
    pub session_id: u64, pub expired_at: Option<i64>,
}
impl Default for TimerState {
    fn default() -> Self { Self { phase: "work".into(), phase_seconds: 1500., remaining: 1500., running: false,
        pomodoro_count: 0, cycle_total: 0, cycle_date: date(now_ms()), rounds: 4, updated_at: 0.,
        task: String::new(), next_task: None, sessions: vec![], session_id: 1, expired_at: None } }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Document {
    pub version: u32, pub revision: u64, pub state: TimerState, pub settings: Settings,
    pub stats: BTreeMap<String, u32>, pub deadline_ms: Option<i64>, pub message: String,
}
impl Default for Document {
    fn default() -> Self { Self { version: 1, revision: 0, state: TimerState::default(), settings: Settings::default(),
        stats: BTreeMap::new(), deadline_ms: None, message: String::new() } }
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Migration { pub state: Option<TimerState>, pub settings: Settings, pub stats: BTreeMap<String, u32> }

pub struct Engine {
    pub doc: Document, path: PathBuf, pub initialized: bool,
    deadline: Option<Instant>, last_tick: Instant, last_checkpoint: Instant,
}
impl Engine {
    pub fn open(path: PathBuf, mono: Instant, wall: i64) -> Result<Self, String> {
        let saved = match std::fs::read(&path) {
            Ok(bytes) => Some(serde_json::from_slice::<Document>(&bytes).map_err(|e| format!("计时文件损坏，已保留原文件：{e}"))?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            Err(e) => return Err(e.to_string()),
        };
        let initialized = saved.is_some();
        let mut engine = Self { doc: saved.unwrap_or_default(), path, initialized,
            deadline: None, last_tick: mono, last_checkpoint: mono };
        if initialized {
            engine.validate()?;
            engine.restore(mono, wall)?;
        }
        Ok(engine)
    }
    fn validate(&self) -> Result<(), String> {
        self.doc.settings.validate()?;
        let s = &self.doc.state;
        if self.doc.version != 1 || !["work", "short_break", "long_break"].contains(&s.phase.as_str())
            || !s.phase_seconds.is_finite() || !(1.0..=86400.0).contains(&s.phase_seconds)
            || !s.remaining.is_finite() || s.remaining < 0. || s.remaining > s.phase_seconds
            || !(1..=12).contains(&s.rounds) {
            return Err("不支持的计时数据，请保留原文件后检查".into());
        }
        Ok(())
    }
    fn write(&self, doc: &Document) -> Result<(), String> {
        let dir = self.path.parent().ok_or("计时文件路径无效")?;
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        let mut file = tempfile::NamedTempFile::new_in(dir).map_err(|e| e.to_string())?;
        let bytes = serde_json::to_vec(doc).map_err(|e| e.to_string())?;
        file.write_all(&bytes).map_err(|e| e.to_string())?;
        file.as_file().sync_all().map_err(|e| e.to_string())?;
        file.persist(&self.path).map_err(|e| format!("计时保存失败：{e}"))?;
        Ok(())
    }
    fn commit(&mut self, mut next: Document, mono: Instant, wall: i64) -> Result<(), String> {
        next.revision = self.doc.revision + 1;
        next.state.updated_at = wall as f64 / 1000.;
        next.deadline_ms = next.state.running.then_some(wall + (next.state.remaining * 1000.).round() as i64);
        // Persist before publishing: a failed write must never acknowledge a completion.
        self.write(&next)?;
        self.deadline = next.state.running.then(|| mono + Duration::from_secs_f64(next.state.remaining));
        self.doc = next;
        self.last_checkpoint = mono;
        Ok(())
    }
    pub fn initialize(&mut self, migration: Migration, mono: Instant, wall: i64) -> Result<(), String> {
        if self.initialized { return Ok(()); }
        migration.settings.validate()?;
        let mut doc = Document { settings: migration.settings, stats: migration.stats, ..Document::default() };
        if let Some(mut state) = migration.state {
            for (i, record) in state.sessions.iter_mut().enumerate() { record.id = i as u64 + 1; }
            state.session_id = state.sessions.len() as u64 + 1;
            state.task = clean_task(&state.task);
            doc.deadline_ms = state.running.then_some(state.expired_at.unwrap_or(
                (state.updated_at * 1000.) as i64 + (state.remaining * 1000.) as i64));
            state.expired_at = None;
            if !state.cycle_date.is_empty() && state.cycle_total > 0 {
                doc.stats.entry(state.cycle_date.clone()).or_insert(state.cycle_total);
            }
            doc.state = state;
        } else {
            doc.state.phase_seconds = doc.settings.seconds("work");
            doc.state.remaining = doc.state.phase_seconds;
        }
        doc.state.rounds = doc.settings.rounds;
        self.doc = doc;
        self.validate()?;
        self.restore(mono, wall)?;
        self.initialized = true;
        Ok(())
    }
    fn restore(&mut self, mono: Instant, wall: i64) -> Result<(), String> {
        let mut next = self.doc.clone();
        if next.state.running {
            let end = next.deadline_ms.unwrap_or(wall);
            next.state.remaining = ((end - wall).max(0) as f64 / 1000.).min(next.state.phase_seconds);
            if next.state.remaining <= 0. {
                finish(&mut next, "completed", end);
                transition(&mut next, true);
                next.state.running = false;
                next.message = "离线期间阶段已到期，已结算一次；下一阶段等待开始".into();
            }
        }
        refresh_day(&mut next, wall);
        self.commit(next, mono, wall)
    }
    pub fn tick(&mut self, mono: Instant, wall: i64) -> Result<bool, String> {
        if !self.initialized { return Ok(false); }
        let gap = mono.saturating_duration_since(self.last_tick);
        self.last_tick = mono;
        let mut next = self.doc.clone();
        let mut changed = false;
        next.message.clear();
        if next.state.running {
            if gap > Duration::from_secs(60) {
                // Do not turn sleep/hibernation into recorded focus time.
                next.state.running = false;
                next.message = "检测到系统挂起，计时已暂停，请确认后继续".into();
                changed = true;
            } else if let Some(end) = self.deadline {
                next.state.remaining = end.saturating_duration_since(mono).as_secs_f64();
                if next.state.remaining <= 0. {
                    let completed_at = wall - mono.saturating_duration_since(end).as_millis() as i64;
                    finish(&mut next, "completed", completed_at);
                    transition(&mut next, true);
                    next.state.running = next.settings.auto_advance;
                    next.message = format!("阶段结束，{}{}", if next.state.running { "开始" } else { "等待开始" }, phase_name(&next.state.phase));
                    changed = true;
                }
            }
        }
        changed |= refresh_day(&mut next, wall);
        if changed || (next.state.running && mono.saturating_duration_since(self.last_checkpoint) >= Duration::from_secs(15)) {
            self.commit(next, mono, wall)?;
        } else { self.doc.state.remaining = next.state.remaining; self.doc.revision += 1; }
        Ok(changed)
    }
    pub fn action(&mut self, action: &str, task: Option<String>, settings: Option<Settings>, mono: Instant, wall: i64) -> Result<(), String> {
        if !self.initialized { return Err("计时数据尚未初始化".into()); }
        self.tick(mono, wall)?;
        let mut next = self.doc.clone();
        next.message.clear();
        match action {
            "toggle" => next.state.running = !next.state.running,
            "skip" => { finish(&mut next, "skipped", wall); transition(&mut next, false); next.state.running = next.settings.auto_advance; }
            "reset" | "wipe" => {
                finish(&mut next, "interrupted", wall);
                if action == "wipe" { next.state.phase = "work".into(); next.state.pomodoro_count = 0; }
                next.state.phase_seconds = next.settings.seconds(&next.state.phase);
                next.state.remaining = next.state.phase_seconds;
                next.state.running = false;
            }
            "task" => {
                if next.state.running || next.state.remaining < next.state.phase_seconds { return Err("当前会话已开始，请设置下一番茄任务".into()); }
                next.state.task = clean_task(&task.unwrap_or_default());
            }
            "next_task" => next.state.next_task = task.map(|t| clean_task(&t)),
            "settings" => {
                let s = settings.ok_or("缺少设置")?;
                s.validate()?;
                next.state.rounds = s.rounds;
                next.settings = s;
            }
            _ => return Err("未知计时操作".into()),
        }
        self.commit(next, mono, wall)
    }
}
fn clean_task(text: &str) -> String { text.chars().filter(|c| !c.is_control()).take(120).collect::<String>().trim().into() }
pub fn phase_name(phase: &str) -> &'static str { match phase { "short_break" => "短休息", "long_break" => "长休息", _ => "专注" } }
fn refresh_day(doc: &mut Document, wall: i64) -> bool {
    let today = date(wall);
    let changed = doc.state.cycle_date != today;
    doc.state.cycle_total = *doc.stats.get(&today).unwrap_or(&0);
    doc.state.cycle_date = today;
    changed
}
fn finish(doc: &mut Document, outcome: &str, wall: i64) {
    let s = &mut doc.state;
    if s.phase == "work" {
        let seconds = (s.phase_seconds - s.remaining).max(0.);
        if seconds > 0. || outcome == "completed" {
            if !s.sessions.iter().any(|r| r.id == s.session_id) {
                s.sessions.push(Record { id: s.session_id, task: s.task.clone(), ended_at: wall, seconds, outcome: outcome.into() });
                if s.sessions.len() > 500 { s.sessions.remove(0); }
                if outcome == "completed" { *doc.stats.entry(date(wall)).or_default() += 1; }
            }
        }
    }
    s.session_id += 1;
}
fn transition(doc: &mut Document, completed: bool) {
    let s = &mut doc.state;
    if s.phase == "work" {
        if completed { s.pomodoro_count += 1; }
        s.phase = if completed && s.pomodoro_count % s.rounds == 0 { "long_break" } else { "short_break" }.into();
    } else {
        if s.phase == "long_break" { s.pomodoro_count = 0; }
        s.phase = "work".into();
        if let Some(task) = s.next_task.take() { s.task = task; }
    }
    s.phase_seconds = doc.settings.seconds(&s.phase);
    s.remaining = s.phase_seconds;
}

#[cfg(test)]
mod tests {
    use super::*;
    fn setup() -> (tempfile::TempDir, Engine, Instant, i64) {
        let dir = tempfile::tempdir().unwrap();
        let mono = Instant::now(); let wall = now_ms();
        let mut engine = Engine::open(dir.path().join("state.json"), mono, wall).unwrap();
        engine.initialize(Migration { state: None, settings: Settings { work_min: 1., ..Settings::default() }, stats: BTreeMap::new() }, mono, wall).unwrap();
        (dir, engine, mono, wall)
    }
    #[test]
    fn skip_obeys_manual_mode_without_counting_completion() {
        let (_dir, mut e, t, w) = setup();
        let settings = Settings { auto_advance: false, ..e.doc.settings.clone() };
        e.action("settings", None, Some(settings), t, w).unwrap();
        e.action("toggle", None, None, t, w).unwrap();
        e.action("skip", None, None, t + Duration::from_secs(10), w + 10000).unwrap();
        assert!(!e.doc.state.running);
        assert_eq!(e.doc.state.phase, "short_break");
        assert_eq!(e.doc.state.sessions[0].outcome, "skipped");
        assert_eq!(e.doc.state.sessions[0].seconds, 10.);
        assert!(e.doc.stats.is_empty());
    }
    #[test]
    fn wall_clock_jump_does_not_change_running_remaining() {
        let (_dir, mut e, t, w) = setup();
        e.action("toggle", None, None, t, w).unwrap();
        e.tick(t + Duration::from_secs(10), w + 3600000).unwrap();
        assert_eq!(e.doc.state.remaining, 50.);
        e.tick(t + Duration::from_secs(20), w - 3600000).unwrap();
        assert_eq!(e.doc.state.remaining, 40.);
    }
    #[test]
    fn offline_completion_is_atomic_and_not_repeated_on_restart() {
        let (dir, mut e, t, w) = setup();
        e.action("toggle", None, None, t, w).unwrap();
        drop(e);
        let e = Engine::open(dir.path().join("state.json"), t + Duration::from_secs(90), w + 90000).unwrap();
        assert_eq!(e.doc.state.sessions.len(), 1);
        assert!(!e.doc.state.running);
        assert_eq!(e.doc.stats.values().sum::<u32>(), 1);
        drop(e);
        let e = Engine::open(dir.path().join("state.json"), t + Duration::from_secs(100), w + 100000).unwrap();
        assert_eq!(e.doc.state.sessions.len(), 1);
        assert_eq!(e.doc.stats.values().sum::<u32>(), 1);
    }
    #[test]
    fn sleep_pauses_without_creating_phantom_work() {
        let (_dir, mut e, t, w) = setup();
        e.action("toggle", None, None, t, w).unwrap();
        e.tick(t + Duration::from_secs(5), w + 5000).unwrap();
        e.tick(t + Duration::from_secs(3600), w + 3600000).unwrap();
        assert!(!e.doc.state.running);
        assert_eq!(e.doc.state.remaining, 55.);
        assert!(e.doc.state.sessions.is_empty());
    }
    #[test]
    fn failed_persistence_does_not_publish_a_completion() {
        let (dir, mut e, t, w) = setup();
        e.action("toggle", None, None, t, w).unwrap();
        let original = e.path.clone();
        let blocker = dir.path().join("not-a-directory");
        std::fs::write(&blocker, b"keep").unwrap();
        e.path = blocker.join("state.json");
        assert!(e.tick(t + Duration::from_secs(60), w + 60000).is_err());
        assert!(e.doc.state.sessions.is_empty());
        assert!(e.doc.stats.is_empty());
        e.path = original;
        e.tick(t + Duration::from_secs(61), w + 61000).unwrap();
        assert_eq!(e.doc.state.sessions.len(), 1);
        assert_eq!(e.doc.stats.values().sum::<u32>(), 1);
    }
    #[test]
    fn next_task_applies_only_to_next_work_phase_and_keeps_record_name() {
        let (_dir, mut e, t, w) = setup();
        e.action("task", Some("当前任务".into()), None, t, w).unwrap();
        e.action("toggle", None, None, t, w).unwrap();
        e.action("next_task", Some("下一任务".into()), None, t, w).unwrap();
        e.tick(t + Duration::from_secs(60), w + 60000).unwrap();
        assert_eq!(e.doc.state.sessions[0].task, "当前任务");
        assert_eq!(e.doc.state.task, "当前任务");
        e.action("skip", None, None, t + Duration::from_secs(61), w + 61000).unwrap();
        assert_eq!(e.doc.state.task, "下一任务");
        assert!(e.doc.state.next_task.is_none());
    }
    #[test]
    fn migration_is_one_time_and_keeps_stats_and_history() {
        let dir = tempfile::tempdir().unwrap();
        let t = Instant::now(); let w = now_ms();
        let mut e = Engine::open(dir.path().join("state.json"), t, w).unwrap();
        let mut legacy = TimerState { task: "新数据".into(), cycle_total: 3, ..TimerState::default() };
        legacy.sessions.push(Record { id: 0, task: "历史任务".into(), ended_at: w - 5000, seconds: 60., outcome: "completed".into() });
        let stats = BTreeMap::from([(date(w), 3)]);
        e.initialize(Migration { state: Some(legacy), settings: Settings::default(), stats }, t, w).unwrap();
        e.initialize(Migration { state: Some(TimerState::default()), settings: Settings::default(), stats: BTreeMap::new() }, t, w).unwrap();
        assert_eq!(e.doc.state.task, "新数据");
        let saved = Engine::open(dir.path().join("state.json"), t, w).unwrap();
        assert_eq!(saved.doc.state.task, "新数据");
        assert_eq!(saved.doc.state.sessions[0].task, "历史任务");
        assert_eq!(saved.doc.stats.values().sum::<u32>(), 3);
        assert!(saved.doc.state.session_id > saved.doc.state.sessions[0].id);
    }
    #[test]
    fn reset_uses_new_duration_and_records_interruption() {
        let (_dir, mut e, t, w) = setup();
        e.action("toggle", None, None, t, w).unwrap();
        let settings = Settings { work_min: 50., ..e.doc.settings.clone() };
        e.action("settings", None, Some(settings), t + Duration::from_secs(10), w + 10000).unwrap();
        e.action("reset", None, None, t + Duration::from_secs(10), w + 10000).unwrap();
        assert_eq!(e.doc.state.remaining, 3000.);
        assert_eq!(e.doc.state.phase_seconds, 3000.);
        assert_eq!(e.doc.state.sessions[0].seconds, 10.);
        assert_eq!(e.doc.state.sessions[0].outcome, "interrupted");
    }
}
