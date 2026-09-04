use std::sync::Mutex;

use tauri::State;

use crate::ble;
use epd42_core::face::{self, FaceState};

#[derive(Default)]
pub struct AppState {
    /// 最近一次成功推送用的设备地址（前端也可自行记忆，这里作为兜底）。
    pub last_address: Mutex<Option<String>>,
}

#[tauri::command]
pub async fn scan_devices(timeout_secs: u64) -> Result<Vec<ble::DeviceInfo>, String> {
    ble::scan_devices(timeout_secs.clamp(3, 60)).await
}

/// 由 Rust 渲染 400x300 沙漏画面（预览与推送共用同一实现）。
#[tauri::command]
pub fn render_face(state: FaceState) -> Result<Vec<u8>, String> {
    Ok(face::render(&state))
}

#[tauri::command]
pub async fn push_frame(
    state: FaceState,
    driver: u8,
    address: Option<String>,
    app_state: State<'_, AppState>,
) -> Result<ble::PushReport, String> {
    let luma = face::render(&state);
    let chosen = match address {
        Some(addr) if !addr.trim().is_empty() => Some(addr.trim().to_string()),
        _ => app_state
            .last_address
            .lock()
            .map(|guard| guard.clone())
            .unwrap_or(None),
    };
    let report = ble::push_frame(chosen.as_deref(), &luma, driver).await?;
    if let Some(addr) = chosen {
        if let Ok(mut guard) = app_state.last_address.lock() {
            *guard = Some(addr);
        }
    }
    Ok(report)
}

#[tauri::command]
pub async fn notify(app: tauri::AppHandle, title: String, body: String) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;

    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn set_autostart(app: tauri::AppHandle, enabled: bool) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;

    if enabled {
        app.autolaunch().enable().map_err(|e| e.to_string())?;
    } else {
        app.autolaunch().disable().map_err(|e| e.to_string())?;
    }
    Ok(app.autolaunch().is_enabled().unwrap_or(false))
}

#[tauri::command]
pub fn get_autostart(app: tauri::AppHandle) -> bool {
    use tauri_plugin_autostart::ManagerExt;

    app.autolaunch().is_enabled().unwrap_or(false)
}

#[tauri::command]
pub fn set_tray_tooltip(app: tauri::AppHandle, text: String) -> Result<(), String> {
    use tauri::tray::TrayIconId;

    if let Some(tray) = app.tray_by_id(&TrayIconId::from("main")) {
        let _ = tray.set_tooltip(Some(text.as_str()));
    }
    Ok(())
}