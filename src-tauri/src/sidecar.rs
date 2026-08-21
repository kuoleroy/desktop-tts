use std::os::windows::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::Ordering;
use std::sync::{mpsc, Arc};
use std::thread;
use tauri::{Emitter, Manager};

use crate::state::{
    log_async, log_error, CommandMessage, SidecarReply, SidecarState, GrabberState,
    GRAB_ENABLED, GRAB_LAST_HWND, LAST_GRAB, NEXT_ID, AppMode, AppState,
    AppSettings, SkipConfig, GrabSkipConfig,
};

pub fn exe_resource_root() -> Option<std::path::PathBuf> {
    let dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    for c in [dir.join("_up_"), dir.clone()] {
        if c.is_dir() {
            return Some(c);
        }
    }
    None
}

pub fn bundled_dir(name: &str) -> Option<std::path::PathBuf> {
    if let Some(root) = exe_resource_root() {
        let p = root.join(name);
        if p.is_dir() {
            return Some(p);
        }
    }
    None
}

pub fn sidecar_script() -> std::path::PathBuf {
    if let Some(dir) = bundled_dir("sidecar") {
        let p = dir.join("tts_sidecar.py");
        if p.exists() {
            return p;
        }
    }
    let dev = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("sidecar")
        .join("tts_sidecar.py");
    if dev.exists() {
        return dev;
    }
    std::path::PathBuf::from("sidecar/tts_sidecar.py")
}

pub fn sidecar_dir() -> std::path::PathBuf {
    sidecar_script().parent().map(|p| p.to_path_buf()).unwrap_or_default()
}

pub fn grabber_script() -> std::path::PathBuf {
    if let Some(dir) = bundled_dir("sidecar") {
        let p = dir.join("tts_grabber.py");
        if p.exists() {
            return p;
        }
    }
    let dev = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("sidecar")
        .join("tts_grabber.py");
    if dev.exists() {
        return dev;
    }
    std::path::PathBuf::from("sidecar/tts_grabber.py")
}

pub fn models_dir() -> std::path::PathBuf {
    if let Some(p) = bundled_dir("models") {
        return p;
    }
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("models")
        .canonicalize()
        .unwrap_or_else(|_| {
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join("models")
        })
}

pub fn cache_dir() -> std::path::PathBuf {
    let p = sidecar_dir()
        .parent()
        .map(|p| p.join("tts_cache"))
        .unwrap_or_else(|| std::path::PathBuf::from("tts_cache"));
    std::fs::canonicalize(&p).unwrap_or(p)
}

pub fn dance_root_dir() -> std::path::PathBuf {
    if let Some(p) = bundled_dir("dance") {
        return p;
    }
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("dance")
        .canonicalize()
        .unwrap_or_else(|_| {
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join("dance")
        })
}

pub fn providers_file() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("voice_providers.json")
}

pub fn diag_log_path() -> std::path::PathBuf {
    if cfg!(debug_assertions) {
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("diag.log")
    } else {
        std::env::var("LOCALAPPDATA")
            .map(|d| std::path::PathBuf::from(d).join("com.kuoleroy.desktop-tts").join("diag.log"))
            .unwrap_or_else(|_| std::path::PathBuf::from("diag.log"))
    }
}

pub fn app_settings_path() -> std::path::PathBuf {
    sidecar_dir().join("settings_app.json")
}

pub fn skip_config_path() -> std::path::PathBuf {
    sidecar_dir().join("skip_apps.json")
}

pub fn grab_skip_config_path() -> std::path::PathBuf {
    sidecar_dir().join("grab_skip_apps.json")
}

pub fn load_app_settings() -> AppSettings {
    std::fs::read_to_string(app_settings_path())
        .ok()
        .and_then(|s| serde_json::from_str::<AppSettings>(&s).ok())
        .unwrap_or_default()
}

pub fn save_app_settings(s: &AppSettings) {
    if let Ok(json) = serde_json::to_string_pretty(s) {
        let _ = std::fs::write(app_settings_path(), json);
    }
}

pub fn read_skip_config() -> SkipConfig {
    let path = skip_config_path();
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub fn write_skip_config(config: &SkipConfig) {
    let path = skip_config_path();
    if let Ok(json) = serde_json::to_string_pretty(config) {
        let _ = std::fs::write(&path, &json);
    }
}

pub fn read_grab_skip_config() -> GrabSkipConfig {
    let path = grab_skip_config_path();
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub fn write_grab_skip_config(config: &GrabSkipConfig) {
    let path = grab_skip_config_path();
    if let Ok(json) = serde_json::to_string_pretty(config) {
        let _ = std::fs::write(&path, &json);
    }
}

pub fn notify_grabber_reload_skip(app: &tauri::AppHandle) {
    let state = app.state::<GrabberState>();
    let guard = state.0.lock().unwrap();
    if let Some((_child, _exit_rx, cmd_tx)) = guard.as_ref() {
        let _ = cmd_tx.send(CommandMessage {
            id: NEXT_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
            cmd: "reload_skip".into(),
            payload: String::new(),
        });
    }
}

pub fn enforce_cache_limit() -> u64 {
    let limit = load_app_settings().cache_limit_mb;
    if limit == 0 {
        return 0;
    }
    let dir = cache_dir();
    let Ok(entries) = std::fs::read_dir(&dir) else { return 0 };
    let mut files: Vec<(std::time::SystemTime, std::path::PathBuf, u64)> = Vec::new();
    for e in entries.flatten() {
        if let Ok(md) = e.metadata() {
            if md.is_file() {
                files.push((md.modified().unwrap_or(std::time::UNIX_EPOCH), e.path(), md.len()));
            }
        }
    }
    let mut total: u64 = files.iter().map(|(_, _, s)| s).sum();
    let limit_bytes = limit * 1024 * 1024;
    let mut removed = 0u64;
    files.sort_by_key(|(t, _, _)| *t);
    for (_, path, size) in files {
        if total <= limit_bytes {
            break;
        }
        if std::fs::remove_file(&path).is_ok() {
            total = total.saturating_sub(size);
            removed += 1;
        }
    }
    if removed > 0 {
        log_async(format!("[cache] cleaned {removed} files (limit {limit}MB)"));
    }
    removed
}

pub fn clamp_to_work_area(
    app: &tauri::AppHandle,
    x: i32,
    y: i32,
    win_w: u32,
    win_h: u32,
) -> (i32, i32) {
    let monitor = app.monitor_from_point(x as f64, y as f64).ok().flatten();
    let Some(m) = monitor else { return (x, y) };
    let pos = m.position();
    let size = m.size();
    let margin = 12;
    let max_x = pos.x as i32 + size.width as i32 - win_w as i32 - margin;
    let max_y = pos.y as i32 + size.height as i32 - win_h as i32 - margin;
    let cx = x.max(pos.x as i32 + margin).min(max_x.max(pos.x as i32 + margin));
    let cy = y.max(pos.y as i32 + margin).min(max_y.max(pos.y as i32 + margin));
    (cx, cy)
}

pub fn spawn_sidecar(app: Arc<tauri::AppHandle>) -> Result<(Child, mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>), String> {
    let py = std::env::var("PYTHON").unwrap_or_else(|_| "python".into());
    let script = sidecar_script();
    let err_log = sidecar_dir().join("sidecar_stderr.log");
    let stderr_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&err_log)
        .map_err(|e| format!("open stderr log {err_log:?}: {e}"))?;
    let mut child = Command::new(&py)
        .arg(&script)
        .creation_flags(0x08000200)
        .current_dir(sidecar_dir())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(stderr_file))
        .spawn()
        .map_err(|e| format!("sidecar spawn failed (python={py}): {e}"))?;

    let (exit_tx, exit_rx) = mpsc::channel::<bool>();
    let (cmd_tx, cmd_rx) = mpsc::channel::<CommandMessage>();

    let stdout = child.stdout.take().expect("sidecar stdout");
    let app2 = Arc::clone(&app);
    thread::spawn(move || {
        use std::io::BufRead;
        let reader = std::io::BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else {
                log_error(&app2, format!("Sidecar stdout error: {line:?}"));
                break;
            };
            let Ok(reply) = serde_json::from_str::<SidecarReply>(&line) else {
                continue;
            };
            if reply.settings.is_some() || reply.voices.is_some() {
                let w = crate::state::SETTINGS_WAITERS.lock().unwrap().remove(&reply.id);
                if let Some(tx) = w {
                    let _ = tx.send(reply.clone());
                    continue;
                }
            }
            let _ = app2.emit("sidecar-reply", &reply);
            log_async(format!("[{}] sidecar reply: {}", std::process::id(), line));
        }
        log_error(&app2, "Sidecar process exited");
        let _ = exit_tx.send(true);
    });

    if let Some(mut stdin) = child.stdin.take() {
        thread::spawn(move || {
            use std::io::Write;
            for msg in cmd_rx {
                let json = serde_json::json!({"id": msg.id, "cmd": msg.cmd, "text": msg.payload});
                if serde_json::to_writer(&mut stdin, &json).is_err() {
                    log_error(&app, "Sidecar stdin write error");
                    break;
                }
                if stdin.write_all(b"\n").is_err() {
                    log_error(&app, "Sidecar stdin write_all error");
                    break;
                }
                if stdin.flush().is_err() {
                    log_error(&app, "Sidecar stdin flush error");
                    break;
                }
            }
            log_error(&app, "Sidecar stdin writer exited");
        });
    } else {
        log_error(&app, "Sidecar stdin unavailable");
    }

    Ok((child, exit_rx, cmd_tx))
}

pub fn spawn_grabber(app: Arc<tauri::AppHandle>) -> Result<(Child, mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>), String> {
    let py = std::env::var("PYTHON").unwrap_or_else(|_| "python".into());
    let script = grabber_script();
    let ppid = std::process::id();
    let err_log = sidecar_dir().join("grabber_stderr.log");
    let stderr_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&err_log)
        .map_err(|e| format!("open grabber stderr log {err_log:?}: {e}"))?;
    let mut child = Command::new(&py)
        .arg(&script)
        .arg("--ppid")
        .arg(ppid.to_string())
        .creation_flags(0x08000200)
        .current_dir(sidecar_dir())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(stderr_file))
        .spawn()
        .map_err(|e| format!("grabber spawn failed (python={py}): {e}"))?;

    let (cmd_tx, cmd_rx) = mpsc::channel::<CommandMessage>();
    if let Some(mut stdin) = child.stdin.take() {
        let app_writer = Arc::clone(&app);
        thread::spawn(move || {
            use std::io::Write;
            for msg in cmd_rx {
                let json = serde_json::json!({"id": msg.id, "cmd": msg.cmd, "text": msg.payload});
                if serde_json::to_writer(&mut stdin, &json).is_err() {
                    log_error(&app_writer, "Grabber stdin write error");
                    break;
                }
                if stdin.write_all(b"\n").is_err() {
                    log_error(&app_writer, "Grabber stdin write_all error");
                    break;
                }
                if stdin.flush().is_err() {
                    log_error(&app_writer, "Grabber stdin flush error");
                    break;
                }
            }
            log_error(&app_writer, "Grabber stdin writer exited");
        });
    } else {
        log_error(&app, "Grabber stdin unavailable");
    }

    let (exit_tx, exit_rx) = mpsc::channel::<bool>();
    let stdout = child.stdout.take().expect("grabber stdout");
    let app2 = Arc::clone(&app);
    thread::spawn(move || {
        use std::io::BufRead;
        let reader = std::io::BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else {
                log_error(&app2, format!("Grabber stdout error: {line:?}"));
                break;
            };
            let Ok(reply) = serde_json::from_str::<SidecarReply>(&line) else {
                continue;
            };
            if reply.grab {
                if let Some(hw) = reply.hwnd {
                    GRAB_LAST_HWND.store(hw as isize, Ordering::Relaxed);
                }
                handle_grab(
                    &app2,
                    reply.text.as_deref().unwrap_or(""),
                    reply.x,
                    reply.y,
                );
            }
            if let Some(exe) = &reply.skip_exe {
                crate::commands::add_skip_app_inner(app2.as_ref().clone(), "exe".into(), exe.clone());
                let _ = app2.emit("skip-app-added", exe);
            }
        }
        log_error(&app2, "Grabber process exited");
        let _ = exit_tx.send(true);
    });

    Ok((child, exit_rx, cmd_tx))
}

pub fn grabber_cmd(app: &tauri::AppHandle, cmd: &str) {
    let state = app.state::<GrabberState>();
    let guard = state.0.lock().unwrap();
    if let Some((_child, _exit_rx, cmd_tx)) = guard.as_ref() {
        let _ = cmd_tx.send(CommandMessage {
            id: NEXT_ID.fetch_add(1, Ordering::Relaxed),
            cmd: cmd.into(),
            payload: String::new(),
        });
        log_async(format!("[grabber] {cmd} requested"));
    } else {
        log_async("[grabber] not running, cannot send cmd".to_string());
    }
}

pub fn grabber_cmd_payload(app: &tauri::AppHandle, cmd: &str, payload: &str) {
    let state = app.state::<GrabberState>();
    let guard = state.0.lock().unwrap();
    if let Some((_child, _exit_rx, cmd_tx)) = guard.as_ref() {
        let _ = cmd_tx.send(CommandMessage {
            id: NEXT_ID.fetch_add(1, Ordering::Relaxed),
            cmd: cmd.into(),
            payload: payload.into(),
        });
        log_async(format!("[grabber] {cmd} payload={payload}"));
    } else {
        log_async("[grabber] not running, cannot send cmd".to_string());
    }
}

pub fn handle_grab(app: &tauri::AppHandle, text: &str, x: Option<i32>, y: Option<i32>) {
    if !GRAB_ENABLED.load(Ordering::Relaxed) {
        return;
    }
    log_async(format!("[{}] grab text ({} chars)", std::process::id(), text.chars().count()));
    let mut last = LAST_GRAB.lock().unwrap();
    let dup = last.elapsed().as_millis() < 1500;
    if !dup {
        *last = std::time::Instant::now();
    }
    {
        let st = app.state::<AppState>();
        let mut guard = st.0.lock().unwrap();
        guard.0 = AppMode::Watch;
        guard.1 = false;
    }
    if let Some(f) = app.get_webview_window("floater") {
        let win_size = f.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(340, 44));
        if !dup {
            if let (Some(px), Some(py)) = (x, y) {
                let (cx, cy) = clamp_to_work_area(app, px + 8, py + 12, win_size.width, win_size.height);
                let _ = f.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
            }
        }
        let _ = f.show();
    }
    let _ = app.emit("floater-text", text);
    let _ = app.emit("grab-text", text);
    let _ = app.emit("toggle-mode", "watch");
}

pub fn send_cmd(app: &tauri::AppHandle, cmd: &str, payload: &str) {
    log_async(format!("[{}] cmd: {} {}", std::process::id(), cmd, payload));
    let app = Arc::new(app.clone());
    let state = app.state::<SidecarState>();

    let cmd_tx_res = {
        let mut guard = state.0.lock().unwrap();
        if let Some((_child, exit_rx, _tx)) = guard.as_ref() {
            if exit_rx.try_recv() == Ok(true) {
                *guard = None;
            }
        }
        if guard.is_none() {
            match spawn_sidecar(Arc::clone(&app)) {
                Ok(s) => { *guard = Some(s); }
                Err(e) => {
                    log_error(&app, format!("{e}"));
                    let _ = app.emit("tts-error", format!("朗读服务启动失败：{e}"));
                    return;
                }
            }
        }
        guard.as_ref().map(|(_c, _e, tx)| tx.clone())
    };

    if let Some(cmd_tx) = cmd_tx_res {
        let msg = CommandMessage {
            id: NEXT_ID.fetch_add(1, Ordering::Relaxed),
            cmd: cmd.to_string(),
            payload: payload.to_string(),
        };
        if let Err(e) = cmd_tx.send(msg) {
            log_error(&app, format!("Failed to send command: {}", e));
        }
    }
}