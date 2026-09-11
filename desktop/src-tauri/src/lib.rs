mod ble;
mod runtime;
mod session;
mod render;
mod tray;
mod notifications;

use runtime::AppState;
use std::{sync::Arc, time::Instant};
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let shortcut_plugin = tauri_plugin_global_shortcut::Builder::new()
        .with_shortcuts(["ctrl+alt+p", "ctrl+alt+s"])
        .expect("注册全局快捷键失败")
        .with_handler(|app, shortcut, event| {
            if event.state == tauri_plugin_global_shortcut::ShortcutState::Pressed {
                use tauri_plugin_global_shortcut::Code;
                if shortcut.key == Code::KeyS {
                    runtime::tray_action(app, true);
                } else if shortcut.key == Code::KeyP {
                    runtime::tray_action(app, false);
                }
            }
        })
        .build();

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
        .plugin(shortcut_plugin)
        .invoke_handler(tauri::generate_handler![
            runtime::scan_devices,
            runtime::render_face,
            runtime::initialize_timer,
            runtime::get_snapshot,
            runtime::timer_action,
            runtime::request_push,
            runtime::cancel_push,
            runtime::get_push_status,
            runtime::set_autostart,
            runtime::get_autostart,
        ])
        .setup(|app| {
            let path = app.path().app_local_data_dir()?.join("sessions-v1.json");
            let engine = session::Engine::open(path, Instant::now(), session::now_ms()).map_err(std::io::Error::other)?;
            let state = Arc::new(AppState::new(engine));
            app.manage(state.clone());
            tray::setup_close_to_tray(app)?;
            tray::setup_tray(app)?;
            runtime::start_background(app.handle().clone(), state);
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("tauri 启动失败");
}
