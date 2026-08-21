#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod state;
mod sidecar;
mod shortcut;
mod commands;

use std::sync::atomic::Ordering;
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use tauri::{Emitter, Listener, Manager};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

use state::{
    log_async, log_error, AppMode, AppState, SidecarState, GrabberState,
    CLICK_THROUGH, GRAB_ENABLED, REGISTERED_EVENTS, LOG_TX, CommandMessage, SidecarReply,
};
use sidecar::{
    spawn_sidecar, spawn_grabber, models_dir, cache_dir, dance_root_dir,
    enforce_cache_limit, load_app_settings, diag_log_path,
};

fn main() {
    // ---- 崩溃捕获 ----
    {
        let log_path = diag_log_path();
        if let Some(parent) = log_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        std::panic::set_hook(Box::new(move |info| {
            use std::io::Write;
            let msg = format!("[{}] PANIC: {}", std::process::id(), info);
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&log_path) {
                let _ = writeln!(f, "{}", msg);
                let _ = writeln!(f, "{}", std::backtrace::Backtrace::force_capture());
            }
            eprintln!("{}", msg);
        }));
    }

    // ---- 异步日志 ----
    let (log_tx, log_rx) = mpsc::channel();
    let _ = LOG_TX.set(log_tx);
    let log_path = diag_log_path();
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    thread::spawn(move || {
        use std::sync::mpsc::RecvTimeoutError;
        let mut buffer: Vec<String> = Vec::new();
        let mut last_write = std::time::Instant::now();
        const FLUSH_INTERVAL: std::time::Duration = std::time::Duration::from_secs(2);
        const FLUSH_LEN: usize = 50;
        loop {
            let got = match log_rx.recv_timeout(std::time::Duration::from_millis(500)) {
                Ok(msg) => { buffer.push(msg); true }
                Err(RecvTimeoutError::Timeout) => false,
                Err(RecvTimeoutError::Disconnected) => break,
            };
            let now = std::time::Instant::now();
            let due = now.duration_since(last_write) >= FLUSH_INTERVAL;
            if (got && (due || buffer.len() >= FLUSH_LEN)) || (!got && due && !buffer.is_empty()) {
                if let Ok(mut f) = std::fs::OpenOptions::new()
                    .create(true).append(true).open(&log_path)
                {
                    use std::io::Write;
                    for line in buffer.drain(..) {
                        let _ = writeln!(f, "{}", line);
                    }
                    last_write = now;
                } else {
                    buffer.clear();
                }
            }
        }
    });

    tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(SidecarState(Mutex::new(None)))
        .manage(GrabberState(Mutex::new(None)))
        .manage(AppState(Mutex::new((AppMode::Watch, false))))
        .invoke_handler(tauri::generate_handler![
            commands::read_text, commands::stop_read, commands::set_voice, commands::set_rate,
            commands::set_pitch, commands::export_mp3, commands::list_models, commands::model_dir,
            commands::quit, commands::toggle_grab, commands::toggle_grab_lock, commands::get_grab_lock,
            commands::show_panel, commands::get_settings, commands::toggle_click_through,
            commands::get_click_through, commands::ocr_rect, commands::show_crop, commands::selread,
            commands::clipwatch,
            commands::get_skip_apps, commands::add_skip_app, commands::remove_skip_app,
            commands::clear_skip_apps, commands::get_fg_window_info, commands::get_window_at,
            commands::get_grab_skip_apps, commands::add_grab_skip_app, commands::remove_grab_skip_app,
            commands::clear_grab_skip_apps,
            commands::get_app_settings, commands::set_app_settings,
            commands::set_main_scale, commands::toggle_panel_ui, commands::get_panel_visible,
            commands::list_dances, commands::open_folder, commands::dance_dir,
            commands::get_providers, commands::save_providers, commands::fetch_provider_voices,
            commands::get_cache_info, commands::set_cache_limit, commands::clear_cache,
            commands::minimize_to_tray,
        ])
        .on_window_event(|window, event| {
            match event {
                tauri::WindowEvent::CloseRequested { .. } => {
                    log_async(format!("[{}] window close-requested: {}", std::process::id(), window.label()));
                }
                tauri::WindowEvent::Destroyed => {
                    log_async(format!("[{}] window destroyed: {}", std::process::id(), window.label()));
                }
                tauri::WindowEvent::Focused(focused) => {
                    log_async(format!("[{}] window focused={} : {}", std::process::id(), focused, window.label()));
                }
                _ => {}
            }
        })
        .setup(|app| {
            let log = |msg: &str| {
                log_async(format!("[{}] {}", std::process::id(), msg));
            };
            log("setup started");

            // ---- 系统托盘 ----
            {
                let show_item = MenuItem::with_id(app, "show", "显示桌宠", true, None::<&str>)?;
                let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
                let menu = Menu::with_items(app, &[&show_item, &quit_item])?;
                let _tray = TrayIconBuilder::with_id("main-tray")
                    .icon(app.default_window_icon().expect("app icon").clone())
                    .menu(&menu)
                    .show_menu_on_left_click(false)
                    .on_menu_event(|app, event| match event.id.as_ref() {
                        "show" => {
                            if let Some(main) = app.get_webview_window("main") {
                                let _ = main.show();
                                let _ = main.set_focus();
                            }
                        }
                        "quit" => commands::quit(app.clone()),
                        _ => {}
                    })
                    .on_tray_icon_event(|tray, event| {
                        if let TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        } = event
                        {
                            let app = tray.app_handle();
                            if let Some(main) = app.get_webview_window("main") {
                                if main.is_visible().unwrap_or(false) {
                                    let _ = main.hide();
                                } else {
                                    let _ = main.show();
                                    let _ = main.set_focus();
                                }
                            }
                        }
                    })
                    .build(app)?;
                log("tray icon created");
            }

            // ---- 单实例限制 ----
            {
                let settings = load_app_settings();
                if !settings.multi_instance {
                    let name: Vec<u16> = "Global\\desktop-tts-single"
                        .encode_utf16()
                        .chain(std::iter::once(0))
                        .collect();
                    unsafe {
                        windows_sys::Win32::Foundation::SetLastError(0);
                        let h = windows_sys::Win32::System::Threading::CreateMutexW(
                            std::ptr::null_mut(), 1, name.as_ptr(),
                        );
                        let exists = h != 0
                            && windows_sys::Win32::Foundation::GetLastError()
                                == windows_sys::Win32::Foundation::ERROR_ALREADY_EXISTS;
                        if exists {
                            let text: Vec<u16> = "已有一个桌面小精灵在运行。\n如需同时运行多个，请在控制面板打开「多开窗口」。"
                                .encode_utf16().chain(std::iter::once(0)).collect();
                            let title: Vec<u16> = "desktop-tts"
                                .encode_utf16().chain(std::iter::once(0)).collect();
                            windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW(
                                0, text.as_ptr(), title.as_ptr(),
                                windows_sys::Win32::UI::WindowsAndMessaging::MB_OK
                                    | windows_sys::Win32::UI::WindowsAndMessaging::MB_ICONINFORMATION,
                            );
                            std::process::exit(0);
                        }
                        let _ = h;
                    }
                }
            }

            let app_handle = Arc::new(app.handle().clone());

            // ---- 动态放行 asset 协议目录 ----
            let scope = app.asset_protocol_scope();
            let _ = scope.allow_directory(models_dir(), true);
            let _ = scope.allow_directory(cache_dir(), true);
            let _ = scope.allow_directory(dance_root_dir(), true);

            // ---- 启动时按上限清理朗读缓存 ----
            enforce_cache_limit();

            // ---- 主窗口穿透状态 ----
            let main_win = app.get_webview_window("main").expect("main window");
            let initial_ct = CLICK_THROUGH.load(Ordering::Relaxed) != 0;
            if let Err(e) = main_win.set_ignore_cursor_events(initial_ct) {
                log_error(&app_handle, format!("Failed to set_ignore_cursor_events: {}", e));
            } else if initial_ct {
                log("Main window click-through (default)");
            } else {
                log("Main window NOT click-through (interactive)");
            }
            log(&format!(
                "main window visible={} inner={:?} outer={:?} pos={:?}",
                main_win.is_visible().unwrap_or(false),
                main_win.inner_size().ok(),
                main_win.outer_size().ok(),
                main_win.outer_position().ok()
            ));

            // ---- 3 秒后复查 ----
            {
                let probe_app = Arc::clone(&app_handle);
                thread::spawn(move || {
                    thread::sleep(std::time::Duration::from_secs(3));
                    let win = probe_app.get_webview_window("main");
                    match win {
                        Some(w) => log_async(format!(
                            "[probe+3s] main visible={} inner={:?} outer={:?} pos={:?}",
                            w.is_visible().unwrap_or(false), w.inner_size().ok(), w.outer_size().ok(), w.outer_position().ok()
                        )),
                        None => log_async("[probe+3s] main window NOT FOUND".into()),
                    }
                    let p = probe_app.get_webview_window("panel");
                    match p {
                        Some(w) => log_async(format!(
                            "[probe+3s] panel visible={} inner={:?}",
                            w.is_visible().unwrap_or(false), w.inner_size().ok()
                        )),
                        None => log_async("[probe+3s] panel window NOT FOUND".into()),
                    }
                });
            }

            // ---- 全屏框选层 ----
            if let Some(crop_win) = app.get_webview_window("crop") {
                let _ = crop_win.set_fullscreen(true);
                let _ = crop_win.hide();
                log("crop window initialized (fullscreen, hidden)");
            }

            log("windows created");
            log(&format!("AppHandle cloned, Arc count: {}", Arc::strong_count(&app_handle)));

            // ---- 全局快捷键 ----
            {
                let settings = load_app_settings();
                CLICK_THROUGH.store(if settings.click_through { 1 } else { 0 }, Ordering::Relaxed);
                if let Some(main) = app.get_webview_window("main") {
                    let _ = main.set_ignore_cursor_events(settings.click_through);
                }
                commands::apply_hotkeys(app.handle(), &settings.hotkey_panel, &settings.hotkey_ct);
            }

            // ---- Sidecar 回复事件 ----
            static LISTENER_SIDECAR_REPLY: &str = "sidecar-reply";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_SIDECAR_REPLY) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_SIDECAR_REPLY);
                let app1 = Arc::clone(&app_handle);
                app.listen_any(LISTENER_SIDECAR_REPLY, move |ev| {
                    if let Ok(reply) = serde_json::from_str::<SidecarReply>(&ev.payload()) {
                        if reply.ok {
                            if let Some(file) = reply.file {
                                let _ = app1.emit("export-done", &file);
                                log_async(format!("[{}] export done: {}", std::process::id(), file));
                                return;
                            }
                            if let Some(mp3) = reply.mp3 {
                                if let Some(main) = app1.get_webview_window("main") {
                                    let path = cache_dir().join(&mp3);
                                    let _ = main.emit("tts", &path.to_string_lossy().to_string());
                                }
                            }
                            if let Some(files) = reply.files.as_ref() {
                                if let Some(main) = app1.get_webview_window("main") {
                                    let paths: Vec<String> = files
                                        .iter()
                                        .map(|f| cache_dir().join(f).to_string_lossy().to_string())
                                        .collect();
                                    let _ = main.emit("tts-multi", &paths);
                                }
                            }
                        } else {
                            if let Some(main) = app1.get_webview_window("main") {
                                let msg = reply.error.unwrap_or_else(|| "TTS 失败".into());
                                let _ = main.emit("tts-error", &msg);
                            }
                        }
                    }
                });
            }

            // ---- 面板关闭事件 ----
            static LISTENER_PANEL_CLOSING: &str = "panel-closing";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_PANEL_CLOSING) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_PANEL_CLOSING);
                let panel_app_handle = Arc::clone(&app_handle);
                app.listen_any(LISTENER_PANEL_CLOSING, move |_| {
                    log("panel-closing received");
                    let app_state = panel_app_handle.state::<AppState>();
                    let mut st = app_state.0.lock().unwrap();
                    let _ = panel_app_handle.get_webview_window("panel").and_then(|w| w.hide().ok());
                    let _ = panel_app_handle.get_webview_window("floater").and_then(|w| w.hide().ok());
                    st.0 = AppMode::Watch;
                    st.1 = false;
                    let _ = panel_app_handle.emit("toggle-mode", "watch");
                    log("panel-closing: emitted watch");
                });
            }

            // ---- 面板就绪 ----
            static LISTENER_PANEL_READY: &str = "panel-ready";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_PANEL_READY) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_PANEL_READY);
                let panel_ready_app_handle = Arc::clone(&app_handle);
                app.listen_any(LISTENER_PANEL_READY, move |_| {
                    log("panel-ready received (default hidden)");
                    let app_state = panel_ready_app_handle.state::<AppState>();
                    let mut st = app_state.0.lock().unwrap();
                    if !st.1 {
                        st.0 = AppMode::Watch;
                        st.1 = false;
                        let _ = panel_ready_app_handle.emit("toggle-mode", "watch");
                        log("panel-ready: keep panel hidden (watch)");
                    }
                });
            }

            // ---- 前端确认回执 ----
            static LISTENER_MODE_CONFIRMED: &str = "mode-confirmed";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_MODE_CONFIRMED) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_MODE_CONFIRMED);
                app.listen_any(LISTENER_MODE_CONFIRMED, move |ev| {
                    log(&format!("frontend mode-confirmed: {}", ev.payload()));
                });
            }

            // ---- 启动 TTS sidecar ----
            {
                let state = app_handle.state::<SidecarState>();
                let mut guard = state.0.lock().unwrap();
                if guard.is_none() {
                    match spawn_sidecar(Arc::clone(&app_handle)) {
                        Ok(s) => *guard = Some(s),
                        Err(e) => {
                            log_error(&app_handle, format!("{e}"));
                            let _ = app_handle.emit("tts-error", format!("朗读服务启动失败：{e}"));
                        }
                    }
                }
            }

            // ---- 启动 grabber ----
            {
                let state = app_handle.state::<GrabberState>();
                let mut guard = state.0.lock().unwrap();
                if guard.is_none() {
                    match spawn_grabber(Arc::clone(&app_handle)) {
                        Ok(s) => {
                            let _ = s.2.send(CommandMessage {
                                id: 0, cmd: "arm".into(), payload: String::new(),
                            });
                            *guard = Some(s);
                        }
                        Err(e) => {
                            log_error(&app_handle, format!("grabber spawn failed: {e}"));
                        }
                    }
                }
            }

            // ---- watchdog: sidecar ----
            {
                let app2 = Arc::clone(&app_handle);
                thread::spawn(move || {
                    loop {
                        std::thread::sleep(std::time::Duration::from_millis(1500));
                        let state = app2.state::<SidecarState>();
                        let mut guard = state.0.lock().unwrap();
                        let dead = match guard.as_ref() {
                            Some((_c, exit_rx, _tx)) => exit_rx.try_recv() == Ok(true),
                            None => true,
                        };
                        if dead {
                            if let Some((mut child, _, _)) = guard.take() {
                                let _ = child.kill();
                                let _ = child.wait();
                            }
                            match spawn_sidecar(Arc::clone(&app2)) {
                                Ok(s) => {
                                    *guard = Some(s);
                                    log_async(format!("[{}] watchdog respawned sidecar", std::process::id()));
                                }
                                Err(e) => {
                                    log_error(&app2, format!("watchdog respawn failed: {e}"));
                                }
                            }
                        }
                    }
                });
            }

            // ---- watchdog: grabber ----
            {
                let app2 = Arc::clone(&app_handle);
                thread::spawn(move || {
                    loop {
                        std::thread::sleep(std::time::Duration::from_millis(1500));
                        let state = app2.state::<GrabberState>();
                        let mut guard = state.0.lock().unwrap();
                        let dead = match guard.as_ref() {
                            Some((_c, exit_rx, _tx)) => exit_rx.try_recv() == Ok(true),
                            None => true,
                        };
                        if dead {
                            if let Some((mut child, _, _)) = guard.take() {
                                let _ = child.kill();
                                let _ = child.wait();
                            }
                            match spawn_grabber(Arc::clone(&app2)) {
                                Ok(s) => {
                                    let cmd = if GRAB_ENABLED.load(Ordering::Relaxed) { "arm" } else { "disarm" };
                                    let _ = s.2.send(CommandMessage {
                                        id: 0, cmd: cmd.into(), payload: String::new(),
                                    });
                                    *guard = Some(s);
                                    log_async(format!("[{}] watchdog respawned grabber ({})", std::process::id(), cmd));
                                }
                                Err(e) => {
                                    log_error(&app2, format!("watchdog grabber respawn failed: {e}"));
                                }
                            }
                        }
                    }
                });
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}