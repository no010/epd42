//! 系统托盘 + 关窗驻托盘。
//!
//! 番茄钟是常驻型应用：关掉窗口不退出（tray 退出），托盘菜单提供
//! 显示窗口 / 暂停继续 / 立即推送 / 退出，气泡提示实时显示当前阶段。

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{App, Emitter, Manager, WindowEvent};

/// 关窗 = 隐藏到托盘，而不是退出（真正退出走托盘菜单）。
pub fn setup_close_to_tray(app: &App) -> tauri::Result<()> {
    let window = app.get_webview_window("main").expect("主窗口缺失");
    let window_for_event = window.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = window_for_event.hide();
        }
    });
    Ok(())
}

pub fn setup_tray(app: &App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
    let toggle = MenuItem::with_id(app, "toggle", "暂停 / 继续", true, None::<&str>)?;
    let push = MenuItem::with_id(app, "push", "立即推送到墨水屏", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &toggle, &push, &quit])?;

    TrayIconBuilder::with_id("main")
        .icon(app.default_window_icon().expect("缺少应用图标").clone())
        .tooltip("EPD42 番茄钟")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main(app),
            "toggle" => {
                let _ = app.emit("menu-toggle", ());
            }
            "push" => {
                let _ = app.emit("menu-push", ());
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}