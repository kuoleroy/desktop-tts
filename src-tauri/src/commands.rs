use std::sync::atomic::Ordering;
use std::sync::Arc;
use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::GlobalShortcutExt;

use crate::state::{
    log_async, AppMode, AppSettings, AppState, SkipConfig, GrabSkipConfig,
    CLICK_THROUGH, GRAB_ENABLED, GRAB_LOCK, GRAB_LAST_HWND, NEXT_ID, CommandMessage,
    SidecarState, GrabberState,
};
use crate::sidecar::{
    self, send_cmd, grabber_cmd, grabber_cmd_payload, load_app_settings, save_app_settings,
    read_skip_config, write_skip_config, read_grab_skip_config, write_grab_skip_config,
    enforce_cache_limit, notify_grabber_reload_skip, providers_file, cache_dir, clamp_to_work_area,
    models_dir, dance_root_dir,
};
use crate::shortcut::parse_shortcut;

// ========== TTS Commands ==========

#[tauri::command]
pub fn read_text(app: tauri::AppHandle, text: String) {
    send_cmd(&app, "tts", &text);
}

#[tauri::command]
pub fn stop_read(app: tauri::AppHandle) {
    send_cmd(&app, "stop", "");
}

#[tauri::command]
pub fn set_voice(app: tauri::AppHandle, name: String) {
    send_cmd(&app, "voice", &name);
    let _ = app.emit("voice-changed", name);
}

#[tauri::command]
pub fn set_rate(app: tauri::AppHandle, rate: i64) {
    send_cmd(&app, "rate", &rate.to_string());
}

#[tauri::command]
pub fn set_pitch(app: tauri::AppHandle, pitch: String) {
    send_cmd(&app, "pitch", &pitch);
}

#[tauri::command]
pub fn export_mp3(app: tauri::AppHandle, text: String) {
    send_cmd(&app, "export", &text);
}

#[tauri::command]
pub fn list_models() -> Vec<String> {
    let models = models_dir();
    std::fs::read_dir(&models)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| {
                    let n = e.file_name().to_string_lossy().to_lowercase();
                    n.ends_with(".vrm") || n.ends_with(".glb")
                })
                .map(|e| models.join(e.file_name()).to_string_lossy().to_string())
                .collect()
        })
        .unwrap_or_default()
}

#[tauri::command]
pub fn model_dir() -> String {
    models_dir().to_string_lossy().to_string()
}

#[tauri::command]
pub fn dance_dir() -> String {
    dance_root_dir().to_string_lossy().to_string()
}

#[tauri::command]
pub fn list_dances() -> Vec<String> {
    let dance_dir = dance_root_dir();
    std::fs::read_dir(&dance_dir)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| e.file_name().to_string_lossy().to_lowercase().ends_with(".vmd"))
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect()
        })
        .unwrap_or_default()
}

#[tauri::command]
pub fn open_folder(path: String) {
    let _ = std::process::Command::new("explorer").arg(&path).spawn();
}

// ========== Grabber Commands ==========

#[tauri::command]
pub fn ocr_rect(app: tauri::AppHandle, rect: String) {
    if !GRAB_ENABLED.load(Ordering::Relaxed) {
        log_async("[grabber] ocr skipped (grab disabled)".to_string());
        return;
    }
    grabber_cmd_payload(&app, "ocr", &rect);
}

#[tauri::command]
pub fn selread(app: tauri::AppHandle) {
    if !GRAB_ENABLED.load(Ordering::Relaxed) {
        log_async("[grabber] selread skipped (grab disabled)".to_string());
        return;
    }
    grabber_cmd(&app, "selread");
}

#[tauri::command]
pub fn clipwatch(app: tauri::AppHandle) {
    if !GRAB_ENABLED.load(Ordering::Relaxed) {
        log_async("[grabber] clipwatch skipped (grab disabled)".to_string());
        return;
    }
    grabber_cmd(&app, "clipwatch");
}

#[tauri::command]
pub fn show_crop(app: tauri::AppHandle) {
    let _ = app.emit("hide-floater", ());
    if let Some(crop) = app.get_webview_window("crop") {
        let _ = crop.set_fullscreen(true);
        let _ = crop.set_always_on_top(true);
        let _ = crop.show();
        let _ = crop.set_focus();
        log_async(format!("[{}] crop window shown", std::process::id()));
    } else {
        log_async("crop window not found".to_string());
    }
}

#[tauri::command]
pub fn toggle_grab(app: tauri::AppHandle, on: bool) {
    GRAB_ENABLED.store(on, Ordering::Relaxed);
    grabber_cmd(&app, if on { "arm" } else { "disarm" });
}

#[tauri::command]
pub fn toggle_grab_lock(app: tauri::AppHandle) -> bool {
    let cur = GRAB_LOCK.load(Ordering::Relaxed);
    if cur != 0 {
        GRAB_LOCK.store(0, Ordering::Relaxed);
        grabber_cmd_payload(&app, "grab_lock", "");
        log_async(format!("[{}] grab lock -> off", std::process::id()));
        false
    } else {
        let h = GRAB_LAST_HWND.load(Ordering::Relaxed);
        if h == 0 {
            log_async(format!("[{}] grab lock: no source window recorded yet", std::process::id()));
            return false;
        }
        GRAB_LOCK.store(h, Ordering::Relaxed);
        grabber_cmd_payload(&app, "grab_lock", &h.to_string());
        log_async(format!("[{}] grab lock -> on (hwnd {})", std::process::id(), h));
        true
    }
}

#[tauri::command]
pub fn get_grab_lock() -> bool {
    GRAB_LOCK.load(Ordering::Relaxed) != 0
}

// ========== Panel / Window Commands ==========

#[tauri::command]
pub fn show_panel(app: tauri::AppHandle) {
    let st = app.state::<AppState>();
    let mut guard = st.0.lock().unwrap();
    if guard.1 {
        return;
    }
    if let (Some(main), Some(panel)) = (
        app.get_webview_window("main"),
        app.get_webview_window("panel"),
    ) {
        if let Ok(pos) = main.outer_position() {
            let win_size = panel.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(420, 600));
            let (cx, cy) = clamp_to_work_area(&app, pos.x + 260, pos.y + 20, win_size.width, win_size.height);
            let _ = panel.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
        }
        let _ = panel.show();
        {
            let p2 = panel.clone();
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(1500));
                if let Ok(t) = p2.title() {
                    log_async(format!("[panel-dbg] title={}", t));
                }
            });
        }
    }
    guard.0 = AppMode::Interact;
    guard.1 = true;
    let _ = app.emit("toggle-mode", "interact");
}

#[tauri::command]
pub fn toggle_panel_ui(app: tauri::AppHandle) {
    toggle_panel(&app);
}

#[tauri::command]
pub fn get_panel_visible(app: tauri::AppHandle) -> bool {
    let st = app.state::<AppState>();
    let guard = st.0.lock().unwrap();
    guard.1
}

#[tauri::command]
pub fn toggle_click_through(app: tauri::AppHandle) -> bool {
    let cur = CLICK_THROUGH.load(Ordering::Relaxed) != 0;
    let next = !cur;
    CLICK_THROUGH.store(next as u64, Ordering::Relaxed);
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.set_ignore_cursor_events(next);
        log_async(format!("[{}] click-through -> {}", std::process::id(), next));
    }
    let _ = app.emit("click-through-changed", next);
    next
}

#[tauri::command]
pub fn get_click_through() -> bool {
    CLICK_THROUGH.load(Ordering::Relaxed) != 0
}

#[tauri::command]
pub fn set_main_scale(app: tauri::AppHandle, scale: f64) {
    let Some(main) = app.get_webview_window("main") else { return };
    let Some(m) = app.primary_monitor().ok().flatten() else { return };
    let wa = m.work_area();
    let size = wa.size;
    let pos = wa.position;
    let w = (size.width as f64 * scale).round().max(120.0) as u32;
    let h = (size.height as f64 * scale).round().max(150.0) as u32;
    let _ = main.set_size(tauri::Size::Physical(tauri::PhysicalSize::new(w, h)));
    let cx = pos.x + (size.width as i32 - w as i32) / 2;
    let cy = pos.y + (size.height as i32 - h as i32) / 2;
    let _ = main.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
    log_async(format!("[{}] set main scale {:.2} -> {}x{}", std::process::id(), scale, w, h));
}

// ========== Settings Commands ==========

#[tauri::command]
pub fn get_settings(app: tauri::AppHandle) -> serde_json::Value {
    let default = serde_json::json!({
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": 0,
        "pitch": "medium"
    });
    let state = app.state::<SidecarState>();
    let tx = {
        let mut guard = state.0.lock().unwrap();
        if let Some((_c, exit_rx, _tx)) = guard.as_ref() {
            if exit_rx.try_recv() == Ok(true) {
                *guard = None;
            }
        }
        if guard.is_none() {
            match sidecar::spawn_sidecar(Arc::new(app.clone())) {
                Ok(s) => { *guard = Some(s); }
                Err(_) => return default,
            }
        }
        guard.as_ref().map(|(_c, _e, tx)| tx.clone())
    };
    let Some(tx) = tx else { return default };

    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let (resp_tx, resp_rx) = std::sync::mpsc::sync_channel::<crate::state::SidecarReply>(1);
    {
        let mut registered = crate::state::SETTINGS_WAITERS.lock().unwrap();
        registered.insert(id, resp_tx);
    }
    if tx.send(CommandMessage { id, cmd: "settings".into(), payload: String::new() }).is_err() {
        return default;
    }
    match resp_rx.recv_timeout(std::time::Duration::from_secs(2)) {
        Ok(r) if r.ok && r.settings.is_some() => r.settings.unwrap(),
        _ => default,
    }
}

#[tauri::command]
pub fn get_providers() -> serde_json::Value {
    std::fs::read_to_string(providers_file())
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .unwrap_or_else(|| serde_json::json!({ "providers": [] }))
}

#[tauri::command]
pub fn save_providers(providers: serde_json::Value) -> serde_json::Value {
    if let Ok(s) = serde_json::to_string_pretty(&providers) {
        if let Some(parent) = providers_file().parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(providers_file(), s);
    }
    providers
}

#[tauri::command]
pub fn fetch_provider_voices(app: tauri::AppHandle, name: String) -> Vec<String> {
    let default = Vec::new();
    let state = app.state::<SidecarState>();
    let tx = {
        let mut guard = state.0.lock().unwrap();
        if let Some((_c, exit_rx, _tx)) = guard.as_ref() {
            if exit_rx.try_recv() == Ok(true) {
                *guard = None;
            }
        }
        if guard.is_none() {
            match sidecar::spawn_sidecar(Arc::new(app.clone())) {
                Ok(s) => { *guard = Some(s); }
                Err(_) => return default,
            }
        }
        guard.as_ref().map(|(_c, _e, tx)| tx.clone())
    };
    let Some(tx) = tx else { return default };

    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let (resp_tx, resp_rx) = std::sync::mpsc::sync_channel::<crate::state::SidecarReply>(1);
    {
        let mut registered = crate::state::SETTINGS_WAITERS.lock().unwrap();
        registered.insert(id, resp_tx);
    }
    if tx.send(CommandMessage { id, cmd: "fetch-voices".into(), payload: name }).is_err() {
        return default;
    }
    match resp_rx.recv_timeout(std::time::Duration::from_secs(35)) {
        Ok(r) if r.ok => r.voices.unwrap_or_default(),
        _ => default,
    }
}

#[tauri::command]
pub fn get_app_settings() -> AppSettings {
    load_app_settings()
}

#[tauri::command]
pub fn set_app_settings(app: tauri::AppHandle, settings: AppSettings) -> Result<(), String> {
    save_app_settings(&settings);
    let ct = settings.click_through;
    CLICK_THROUGH.store(ct as u64, Ordering::Relaxed);
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.set_ignore_cursor_events(ct);
    }
    let _ = app.emit("click-through-changed", ct);
    apply_hotkeys(&app, &settings.hotkey_panel, &settings.hotkey_ct);
    let _ = app.emit("floater-style-changed", serde_json::json!({
        "color": settings.floater_color,
        "opacity": settings.floater_opacity,
    }));
    Ok(())
}

// ========== Cache Commands ==========

#[tauri::command]
pub fn get_cache_info() -> serde_json::Value {
    let limit = load_app_settings().cache_limit_mb;
    enforce_cache_limit();
    let dir = cache_dir();
    let mut size = 0u64;
    let mut files = 0u64;
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for e in entries.flatten() {
            if let Ok(md) = e.metadata() {
                if md.is_file() {
                    size += md.len();
                    files += 1;
                }
            }
        }
    }
    serde_json::json!({
        "size_mb": size as f64 / (1024.0 * 1024.0),
        "files": files,
        "limit_mb": limit,
        "dir": dir.to_string_lossy(),
    })
}

#[tauri::command]
pub fn set_cache_limit(mb: u64) {
    let mut s = load_app_settings();
    s.cache_limit_mb = mb;
    save_app_settings(&s);
    enforce_cache_limit();
}

#[tauri::command]
pub fn clear_cache() -> u64 {
    let dir = cache_dir();
    let mut removed = 0u64;
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for e in entries.flatten() {
            if let Ok(md) = e.metadata() {
                if md.is_file() && std::fs::remove_file(e.path()).is_ok() {
                    removed += 1;
                }
            }
        }
    }
    log_async(format!("[cache] manual clear removed {removed} files"));
    removed
}

// ========== Skip App Commands ==========

#[tauri::command]
pub fn get_skip_apps() -> SkipConfig {
    read_skip_config()
}

#[tauri::command]
pub fn add_skip_app(app: tauri::AppHandle, app_type: String, name: String) {
    add_skip_app_inner(app, app_type, name);
}

pub fn add_skip_app_inner(app: tauri::AppHandle, app_type: String, name: String) {
    let mut config = read_skip_config();
    match app_type.as_str() {
        "class" => {
            if !config.skip_window_classes.contains(&name) {
                config.skip_window_classes.push(name);
            }
        }
        "exe" => {
            if !config.skip_exe_names.contains(&name) {
                config.skip_exe_names.push(name.to_lowercase());
            }
        }
        _ => return,
    }
    write_skip_config(&config);
    notify_grabber_reload_skip(&app);
}

#[tauri::command]
pub fn remove_skip_app(app: tauri::AppHandle, app_type: String, name: String) {
    let mut config = read_skip_config();
    match app_type.as_str() {
        "class" => config.skip_window_classes.retain(|c| c != &name),
        "exe" => config.skip_exe_names.retain(|e| e != &name.to_lowercase()),
        _ => return,
    }
    write_skip_config(&config);
    notify_grabber_reload_skip(&app);
}

#[tauri::command]
pub fn clear_skip_apps(app: tauri::AppHandle) {
    write_skip_config(&SkipConfig::default());
    notify_grabber_reload_skip(&app);
}

// ========== Grab Skip App Commands ==========

#[tauri::command]
pub fn get_grab_skip_apps() -> GrabSkipConfig {
    read_grab_skip_config()
}

#[tauri::command]
pub fn add_grab_skip_app(app: tauri::AppHandle, app_type: String, name: String) {
    let mut config = read_grab_skip_config();
    match app_type.as_str() {
        "class" => {
            if !config.grab_skip_window_classes.contains(&name) {
                config.grab_skip_window_classes.push(name);
            }
        }
        "exe" => {
            if !config.grab_skip_exe_names.contains(&name) {
                config.grab_skip_exe_names.push(name.to_lowercase());
            }
        }
        _ => return,
    }
    write_grab_skip_config(&config);
    notify_grabber_reload_skip(&app);
}

#[tauri::command]
pub fn remove_grab_skip_app(app: tauri::AppHandle, app_type: String, name: String) {
    let mut config = read_grab_skip_config();
    match app_type.as_str() {
        "class" => config.grab_skip_window_classes.retain(|c| c != &name),
        "exe" => config.grab_skip_exe_names.retain(|e| e != &name.to_lowercase()),
        _ => return,
    }
    write_grab_skip_config(&config);
    notify_grabber_reload_skip(&app);
}

#[tauri::command]
pub fn clear_grab_skip_apps(app: tauri::AppHandle) {
    write_grab_skip_config(&GrabSkipConfig::default());
    notify_grabber_reload_skip(&app);
}

// ========== Window Info Commands ==========

#[tauri::command]
pub fn get_fg_window_info() -> serde_json::Value {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::OpenProcess;
    use windows_sys::Win32::UI::WindowsAndMessaging::{GetClassNameW, GetForegroundWindow, GetWindowThreadProcessId};

    let mut class = String::new();
    let mut exe = String::new();

    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd != 0 {
            let mut buf = [0u16; 256];
            let len = GetClassNameW(hwnd, buf.as_mut_ptr(), 256);
            if len > 0 {
                class = String::from_utf16_lossy(&buf[..len as usize]);
            }
            let mut pid: u32 = 0;
            let _ = GetWindowThreadProcessId(hwnd, &mut pid);
            if pid != 0 {
                let h = OpenProcess(0x1000, 0, pid);
                if h != 0 {
                    let mut exe_buf = [0u16; 1024];
                    let mut size = 1024u32;
                    if windows_sys::Win32::System::Threading::QueryFullProcessImageNameW(h, 0, exe_buf.as_mut_ptr(), &mut size) != 0 {
                        let path = String::from_utf16_lossy(&exe_buf[..size as usize]);
                        if let Some(base) = std::path::Path::new(&path).file_name() {
                            exe = base.to_string_lossy().to_lowercase();
                        }
                    }
                    CloseHandle(h);
                }
            }
        }
    }

    serde_json::json!({ "class": class, "exe": exe })
}

#[tauri::command]
pub fn get_window_at() -> serde_json::Value {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::OpenProcess;
    use windows_sys::Win32::UI::WindowsAndMessaging::{GetAncestor, GetClassNameW, GetCursorPos, GetWindowThreadProcessId, WindowFromPoint};

    let mut class = String::new();
    let mut exe = String::new();
    let mut hwnd = 0usize;

    unsafe {
        let mut pt: windows_sys::Win32::Foundation::POINT = std::mem::zeroed();
        GetCursorPos(&mut pt);
        let w = WindowFromPoint(pt);
        if w != 0 {
            hwnd = GetAncestor(w, 2) as usize;
        }
        if hwnd != 0 {
            let mut buf = [0u16; 256];
            let len = GetClassNameW(hwnd as _, buf.as_mut_ptr(), 256);
            if len > 0 {
                class = String::from_utf16_lossy(&buf[..len as usize]);
            }
            let mut pid: u32 = 0;
            let _ = GetWindowThreadProcessId(hwnd as _, &mut pid);
            if pid != 0 {
                let h = OpenProcess(0x1000, 0, pid);
                if h != 0 {
                    let mut exe_buf = [0u16; 1024];
                    let mut size = 1024u32;
                    if windows_sys::Win32::System::Threading::QueryFullProcessImageNameW(h, 0, exe_buf.as_mut_ptr(), &mut size) != 0 {
                        let path = String::from_utf16_lossy(&exe_buf[..size as usize]);
                        if let Some(base) = std::path::Path::new(&path).file_name() {
                            exe = base.to_string_lossy().to_lowercase();
                        }
                    }
                    CloseHandle(h);
                }
            }
        }
    }

    serde_json::json!({ "class": class, "exe": exe })
}

// ========== Quit / Tray ==========

#[tauri::command]
pub fn minimize_to_tray(app: tauri::AppHandle) {
    for label in ["main", "panel", "floater"] {
        if let Some(w) = app.get_webview_window(label) {
            let _ = w.hide();
        }
    }
}

#[tauri::command]
pub fn quit(app: tauri::AppHandle) {
    {
        let state = app.state::<SidecarState>();
        let mut guard = state.0.lock().unwrap();
        if let Some((child, _, _)) = guard.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        *guard = None;
    }
    {
        let state = app.state::<GrabberState>();
        let mut guard = state.0.lock().unwrap();
        if let Some((child, _, _)) = guard.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        *guard = None;
    }
    app.exit(0);
}

// ========== Internal Helpers (used by main.rs setup) ==========

pub fn toggle_panel(app: &tauri::AppHandle) {
    let app_state = app.state::<AppState>();
    let mut st = app_state.0.lock().unwrap();
    let Some(panel) = app.get_webview_window("panel") else { return };
    let Some(main) = app.get_webview_window("main") else { return };
    if st.1 {
        let _ = panel.hide();
        st.0 = AppMode::Watch;
        let _ = app.emit("toggle-mode", "watch");
        log_async("emit toggle-mode watch".into());
        st.1 = false;
    } else {
        if let Ok(pos) = main.outer_position() {
            let win_size = panel.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(420, 600));
            let (cx, cy) = clamp_to_work_area(app, pos.x + 260, pos.y + 20, win_size.width, win_size.height);
            let _ = panel.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
        }
        let _ = panel.show();
        let _ = panel.set_focus();
        st.0 = AppMode::Interact;
        st.1 = true;
        let _ = app.emit("toggle-mode", "interact");
        log_async("emit toggle-mode interact".into());
    }
}

pub fn toggle_ct(app: &tauri::AppHandle) {
    let cur = CLICK_THROUGH.load(Ordering::Relaxed) != 0;
    let next = !cur;
    CLICK_THROUGH.store(next as u64, Ordering::Relaxed);
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.set_ignore_cursor_events(next);
    }
    log_async(format!("hotkey: click-through -> {}", next));
    let _ = app.emit("click-through-changed", next);
}

pub fn apply_hotkeys(app: &tauri::AppHandle, panel_shortcut: &str, ct_shortcut: &str) {
    let _ = app.global_shortcut().unregister_all();
    use tauri_plugin_global_shortcut::{Shortcut, ShortcutState};
    if let Some((m, c)) = parse_shortcut(panel_shortcut) {
        let shortcut = Shortcut::new(Some(m), c);
        let _ = app.global_shortcut().on_shortcut(shortcut, move |app, _s, event| {
            if event.state() != ShortcutState::Pressed { return; }
            toggle_panel(app);
        });
        log_async(format!("hotkey panel registered: {}", panel_shortcut));
    } else {
        log_async(format!("hotkey panel invalid: {}", panel_shortcut));
    }
    if let Some((m, c)) = parse_shortcut(ct_shortcut) {
        let shortcut = Shortcut::new(Some(m), c);
        let _ = app.global_shortcut().on_shortcut(shortcut, move |app, _s, event| {
            if event.state() != ShortcutState::Pressed { return; }
            toggle_ct(app);
        });
        log_async(format!("hotkey ct registered: {}", ct_shortcut));
    } else {
        log_async(format!("hotkey ct invalid: {}", ct_shortcut));
    }
}