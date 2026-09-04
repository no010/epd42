mod ble;
mod commands;
mod tray;

use commands::AppState;
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // 双开时把已有窗口调出来，避免两个计时器同时抢推一块屏
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec![]),
        ))
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            commands::scan_devices,
            commands::push_frame,
            commands::notify,
            commands::set_autostart,
            commands::get_autostart,
            commands::set_tray_tooltip,
        ])
        .setup(|app| {
            tray::setup_close_to_tray(app)?;
            tray::setup_tray(app)?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("tauri 启动失败");
}