mod ble;
mod commands;

use commands::AppState;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            commands::scan_devices,
            commands::push_frame,
            commands::notify,
        ])
        .run(tauri::generate_context!())
        .expect("tauri 启动失败");
}