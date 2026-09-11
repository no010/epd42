//! Use our own Windows identity, including development and portable builds.
#[cfg(windows)]
pub fn show(app: &tauri::AppHandle, title: &str, body: &str) -> Result<(), String> {
    use tauri::Manager;
    use tauri_winrt_notification::Toast;
    use winreg::{enums::HKEY_CURRENT_USER, RegKey};

    let id = &app.config().identifier;
    let assets = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&assets).map_err(|e| e.to_string())?;
    let icon = assets.join("notification-icon.png");
    if !icon.exists() {
        std::fs::write(&icon, include_bytes!("../icons/128x128.png"))
            .map_err(|e| e.to_string())?;
    }
    let (key, _) = RegKey::predef(HKEY_CURRENT_USER)
        .create_subkey(format!(r"Software\Classes\AppUserModelId\{id}"))
        .map_err(|e| e.to_string())?;
    key.set_value("DisplayName", &"EPD42 番茄钟").map_err(|e| e.to_string())?;
    key.set_value("IconUri", &icon.to_string_lossy().as_ref()).map_err(|e| e.to_string())?;
    Toast::new(id).title(title).text1(body).show().map_err(|e| e.to_string())
}

#[cfg(not(windows))]
pub fn show(app: &tauri::AppHandle, title: &str, body: &str) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;
    app.notification().builder().title(title).body(body).show().map_err(|e| e.to_string())
}
