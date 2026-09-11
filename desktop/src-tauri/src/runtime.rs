use std::{sync::{Mutex, Arc}, time::{Duration, Instant}};
use tauri::{Emitter, Manager, State};
use tokio::sync::{Notify, watch};
use serde::Serialize;
use crate::{ble, render, session::{self, Document, Engine, Migration, Settings}};

#[derive(Clone, Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct PushStatus { pub busy: bool, pub message: String, pub last_success: Option<i64> }
#[derive(Default)]
struct PushControl { pending: bool, status: PushStatus }
pub struct AppState {
    pub engine: Mutex<Engine>,
    pub ble_lock: tokio::sync::Mutex<()>,
    push: Mutex<PushControl>,
    wake: Notify,
    cancel: watch::Sender<u64>,
}
impl AppState {
    pub fn new(engine: Engine) -> Self {
        let (cancel, _) = watch::channel(0);
        Self { engine: Mutex::new(engine), ble_lock: tokio::sync::Mutex::new(()),
            push: Mutex::new(PushControl::default()), wake: Notify::new(), cancel }
    }
    fn snapshot(&self) -> Result<Document, String> {
        self.engine.lock().map(|e| e.doc.clone()).map_err(|e| e.to_string())
    }
    pub fn request_push(&self) {
        if let Ok(mut p) = self.push.lock() { p.pending = true; }
        self.wake.notify_one();
    }
    fn cancel_push(&self) {
        if let Ok(mut p) = self.push.lock() {
            p.pending = false;
            p.status.message = if p.status.busy { "正在取消并断开连接…" } else { "推送已取消" }.into();
            self.cancel.send_modify(|generation| *generation += 1);
        }
    }
    fn take_push(&self) -> Option<watch::Receiver<u64>> {
        let mut p = self.push.lock().ok()?;
        if !p.pending { return None; }
        p.pending = false;
        p.status.busy = true;
        Some(self.cancel.subscribe())
    }
    fn publish_push(&self, app: &tauri::AppHandle) {
        if let Ok(p) = self.push.lock() { let _ = app.emit("push-status", p.status.clone()); }
    }
    fn status(&self, app: &tauri::AppHandle, message: String) {
        if let Ok(mut p) = self.push.lock() { p.status.message = message; }
        self.publish_push(app);
    }
}

#[tauri::command]
pub fn initialize_timer(migration: Migration, app_state: State<'_, Arc<AppState>>) -> Result<Document, String> {
    let mut engine = app_state.engine.lock().map_err(|e| e.to_string())?;
    engine.initialize(migration, Instant::now(), session::now_ms())?;
    Ok(engine.doc.clone())
}
#[tauri::command]
pub fn get_snapshot(app_state: State<'_, Arc<AppState>>) -> Result<Document, String> { app_state.snapshot() }
#[tauri::command]
pub fn timer_action(kind: String, task: Option<String>, settings: Option<Settings>, app: tauri::AppHandle,
    app_state: State<'_, Arc<AppState>>) -> Result<Document, String> {
    act(&app_state, &app, &kind, task, settings)
}
pub fn act(state: &AppState, app: &tauri::AppHandle, kind: &str, task: Option<String>, settings: Option<Settings>) -> Result<Document, String> {
    let doc = {
        let mut engine = state.engine.lock().map_err(|e| e.to_string())?;
        engine.action(kind, task, settings, Instant::now(), session::now_ms())?;
        engine.doc.clone()
    };
    if kind == "settings" { state.cancel_push(); state.publish_push(app); }
    if doc.settings.push_enabled && !["task", "next_task"].contains(&kind) { state.request_push(); }
    let _ = app.emit("timer-state", doc.clone());
    Ok(doc)
}
#[tauri::command]
pub async fn scan_devices(timeout_secs: u64, app_state: State<'_, Arc<AppState>>) -> Result<Vec<ble::DeviceInfo>, String> {
    let _guard = app_state.ble_lock.lock().await;
    tokio::time::timeout(Duration::from_secs(70), ble::scan_devices(timeout_secs.clamp(3, 60))).await
        .map_err(|_| "扫描超时".to_string())?
}
#[tauri::command]
pub fn render_face(app_state: State<'_, Arc<AppState>>) -> Result<Vec<u8>, String> { render::frame(&app_state.snapshot()?) }
#[tauri::command]
pub fn request_push(app_state: State<'_, Arc<AppState>>) { app_state.request_push(); }
#[tauri::command]
pub fn cancel_push(app: tauri::AppHandle, app_state: State<'_, Arc<AppState>>) {
    app_state.cancel_push(); app_state.publish_push(&app);
}
#[tauri::command]
pub fn get_push_status(app_state: State<'_, Arc<AppState>>) -> Result<PushStatus, String> {
    app_state.push.lock().map(|p| p.status.clone()).map_err(|e| e.to_string())
}
#[tauri::command]
pub fn set_autostart(app: tauri::AppHandle, enabled: bool) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    if enabled { app.autolaunch().enable() } else { app.autolaunch().disable() }.map_err(|e| e.to_string())?;
    app.autolaunch().is_enabled().map_err(|e| e.to_string())
}
#[tauri::command]
pub fn get_autostart(app: tauri::AppHandle) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    app.autolaunch().is_enabled().map_err(|e| e.to_string())
}

pub fn start_background(app: tauri::AppHandle, state: Arc<AppState>) {
    let clock_app = app.clone();
    let clock_state = state.clone();
    std::thread::spawn(move || {
        let mut next_push = Instant::now();
        let mut last_emit = Instant::now();
        let mut last_error = String::new();
        loop {
            std::thread::sleep(Duration::from_millis(250));
            let now = Instant::now();
            let result = clock_state.engine.lock().map_err(|e| e.to_string()).and_then(|mut engine| {
                let changed = engine.tick(now, session::now_ms())?;
                Ok((changed, engine.doc.clone(), engine.initialized))
            });
            let (changed, doc, initialized) = match result {
                Ok(value) => { last_error.clear(); value },
                Err(error) => { if error != last_error { let _ = clock_app.emit("runtime-error", &error); last_error = error; } continue; }
            };
            if !initialized { continue; }
            if changed && !doc.message.is_empty() {
                if let Err(e) = crate::notifications::show(&clock_app, "EPD42 番茄钟", &doc.message) {
                    let _ = clock_app.emit("runtime-error", format!("通知失败：{e}"));
                }
            }
            let interval = Duration::from_secs_f64(doc.settings.push_interval * 60.);
            if doc.settings.push_enabled && (changed || (doc.state.running && !interval.is_zero() && now >= next_push)) {
                clock_state.request_push();
                next_push = now + interval;
            }
            if changed || now.duration_since(last_emit) >= Duration::from_secs(1) {
                let text = format!("{} {} {:02}:{:02} · 今日 {} 个", if doc.state.running { "▶" } else { "⏸" },
                    session::phase_name(&doc.state.phase), doc.state.remaining.ceil() as u64 / 60,
                    doc.state.remaining.ceil() as u64 % 60, doc.state.cycle_total);
                if let Some(tray) = clock_app.tray_by_id("main") { let _ = tray.set_tooltip(Some(&text)); }
                let _ = clock_app.emit("timer-state", doc);
                last_emit = now;
            }
        }
    });
    tauri::async_runtime::spawn(async move {
        loop {
            state.wake.notified().await;
            loop {
                let Some(mut cancel) = state.take_push() else { break; };
                let generation = *cancel.borrow();
                let mut success = false;
                for attempt in 1..=4 {
                    if *cancel.borrow() != generation { break; }
                    state.status(&app, format!("正在同步（第 {attempt}/4 次）…"));
                    let result = transfer(&state, cancel.clone()).await;
                    if *cancel.borrow() != generation { break; }
                    match result {
                        Ok(report) => {
                            if let Ok(mut p) = state.push.lock() { p.status.last_success = Some(session::now_ms()); }
                            state.status(&app, format!("同步成功 · {} 平面 / {} 包", report.planes, report.packets));
                            success = true; break;
                        }
                        Err(error) => {
                            state.status(&app, if attempt == 4 { format!("推送失败：{error}") }
                                else { format!("第 {attempt} 次失败，1.5 秒后重试：{error}") });
                            if attempt < 4 {
                                tokio::select! { biased; _ = cancel.changed() => { break; }, _ = tokio::time::sleep(Duration::from_millis(1500)) => {} }
                            }
                        }
                    }
                }
                if let Ok(mut p) = state.push.lock() {
                    p.status.busy = false;
                    if !success && *cancel.borrow() != generation { p.status.message = "推送已取消".into(); }
                }
                state.publish_push(&app);
            }
        }
    });
}
async fn transfer(state: &AppState, mut cancel: watch::Receiver<u64>) -> Result<ble::PushReport, String> {
    let _guard = tokio::select! { biased; _ = cancel.changed() => return Err("推送已取消".into()), guard = state.ble_lock.lock() => guard };
    let doc = state.snapshot()?;
    let luma = render::frame(&doc)?;
    ble::push_frame(doc.settings.address.as_deref(), &luma, doc.settings.driver.parse().map_err(|_| "无效驱动")?,
        doc.settings.scan_timeout, cancel).await
}

pub fn tray_action(app: &tauri::AppHandle, push: bool) {
    let Some(state) = app.try_state::<Arc<AppState>>() else { return; };
    if push { state.request_push(); }
    else if let Err(e) = act(&state, app, "toggle", None, None) { let _ = app.emit("runtime-error", e); }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test(flavor = "current_thread")]
    async fn cancel_waiting_for_ble_never_starts_transfer() {
        let dir = tempfile::tempdir().unwrap();
        let state = AppState::new(Engine::open(dir.path().join("state.json"), Instant::now(), session::now_ms()).unwrap());
        let _busy = state.ble_lock.lock().await;
        state.request_push();
        let receiver = state.take_push().unwrap();
        state.request_push();
        state.cancel_push();
        assert!(state.take_push().is_none(), "cancel also removes the queued follow-up");
        let result = tokio::time::timeout(Duration::from_millis(100), transfer(&state, receiver)).await.unwrap();
        assert_eq!(result.unwrap_err(), "推送已取消");
    }
    #[test]
    fn coalesces_changes_and_allows_manual_push_after_cancel() {
        let dir = tempfile::tempdir().unwrap();
        let state = AppState::new(Engine::open(dir.path().join("state.json"), Instant::now(), session::now_ms()).unwrap());
        state.request_push(); state.request_push(); state.request_push();
        let old = state.take_push().unwrap();
        assert!(state.take_push().is_none());
        state.cancel_push();
        assert!(old.has_changed().unwrap());
        state.request_push();
        assert!(!state.take_push().unwrap().has_changed().unwrap());
    }
}
