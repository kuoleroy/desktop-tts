#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use tauri::{Emitter, Listener, Manager};
use tauri_plugin_global_shortcut::GlobalShortcutExt;

static REGISTERED_EVENTS: std::sync::LazyLock<Mutex<HashSet<&'static str>>> =
    std::sync::LazyLock::new(|| Mutex::new(HashSet::new()));

static LOG_TX: std::sync::OnceLock<mpsc::Sender<String>> = std::sync::OnceLock::new();

fn log_async(msg: String) {
    if let Some(tx) = LOG_TX.get() {
        let _ = tx.send(msg);
    }
}

fn log_error(_app: &Arc<tauri::AppHandle>, msg: impl AsRef<str>) {
    log_async(format!("[{}] ERROR: {}", std::process::id(), msg.as_ref()));
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AppMode {
    Watch,
    Interact,
}

#[derive(Clone)]
struct CommandMessage {
    id: u64,
    cmd: String,
    payload: String,
}

struct SidecarState(Mutex<Option<(Child, std::sync::mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>)>>);
struct AppState(Mutex<(AppMode, bool)>);

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Serialize, Deserialize, Clone, Debug)]
struct SidecarReply {
    id: u64,
    ok: bool,
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mp3: Option<String>,
}

/// dev 模式：从项目目录找 sidecar 脚本；release 模式：exe 旁 sidecar 目录
fn sidecar_script() -> std::path::PathBuf {
    if cfg!(debug_assertions) {
        let dev = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("sidecar")
            .join("tts_sidecar.py");
        if dev.exists() {
            return dev;
        }
    }
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("sidecar").join("tts_sidecar.py")))
        .unwrap_or_else(|| std::path::PathBuf::from("sidecar/tts_sidecar.py"))
}

fn sidecar_dir() -> std::path::PathBuf {
    sidecar_script().parent().map(|p| p.to_path_buf()).unwrap_or_default()
}

fn spawn_sidecar(app: Arc<tauri::AppHandle>) -> (Child, std::sync::mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>) {
    let py = std::env::var("PYTHON").unwrap_or_else(|_| "python".into());
    let script = sidecar_script();
    let mut child = Command::new(py)
        .arg(&script)
        .current_dir(sidecar_dir())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("sidecar spawn failed");

    let (exit_tx, exit_rx) = std::sync::mpsc::channel::<bool>();
    let (cmd_tx, cmd_rx) = mpsc::channel::<CommandMessage>();

    // stdout 读取线程 → 解析 NDJSON → emit 到前端
    let stdout = child.stdout.take().expect("sidecar stdout");
    let app2 = Arc::clone(&app);
    thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else {
                log_error(&app2, "Sidecar stdout closed/error");
                break;
            };
            let Ok(reply) = serde_json::from_str::<SidecarReply>(&line) else {
                continue;
            };
            let _ = app2.emit("sidecar-reply", &reply);
            log_async(format!("[{}] sidecar reply: {}", std::process::id(), line));
        }
        log_error(&app2, "Sidecar process exited");
        let _ = exit_tx.send(true);
    });

    // stdin 写入线程 → 从命令队列读取 NDJSON 指令写入 sidecar stdin
    if let Some(mut stdin) = child.stdin.take() {
        thread::spawn(move || {
            use std::io::Write;
            for msg in cmd_rx {
                let json = serde_json::json!({"id": msg.id, "cmd": msg.cmd, "text": msg.payload});
                if serde_json::to_writer(&mut stdin, &json).is_err() {
                    break;
                }
                if stdin.write_all(b"\n").is_err() {
                    break;
                }
                if stdin.flush().is_err() {
                    break;
                }
            }
        });
    } else {
        log_error(&app, "Sidecar stdin unavailable");
    }

    (child, exit_rx, cmd_tx)
}

fn send_cmd(app: &tauri::AppHandle, cmd: &str, payload: &str) {
    log_async(format!("[{}] cmd: {} {}", std::process::id(), cmd, payload));
    let app = Arc::new(app.clone());
    let state = app.state::<SidecarState>();
    let mut guard = state.0.lock().unwrap();
    if guard.is_none() {
        let (child, exit_rx, cmd_tx) = spawn_sidecar(Arc::clone(&app));
        *guard = Some((child, exit_rx, cmd_tx));
    }
    if let Some((_child, exit_rx, cmd_tx)) = guard.as_mut() {
        let msg = CommandMessage {
            id: NEXT_ID.fetch_add(1, Ordering::Relaxed),
            cmd: cmd.to_string(),
            payload: payload.to_string(),
        };
        if let Err(e) = cmd_tx.send(msg) {
            log_error(&app, format!("Failed to send command: {}", e));
        }
        if let Ok(exited) = exit_rx.try_recv() {
            if exited {
                log_error(&app, "Sidecar process exited, restarting...");
                let (new_child, new_rx, new_tx) = spawn_sidecar(Arc::clone(&app));
                *guard = Some((new_child, new_rx, new_tx));
            }
        }
    }
}

#[tauri::command]
fn read_text(app: tauri::AppHandle, text: String) {
    send_cmd(&app, "tts", &text);
}

#[tauri::command]
fn stop_read(app: tauri::AppHandle) {
    send_cmd(&app, "stop", "");
}

#[tauri::command]
fn set_voice(app: tauri::AppHandle, name: String) {
    send_cmd(&app, "voice", &name);
}

#[tauri::command]
fn set_rate(app: tauri::AppHandle, rate: i64) {
    send_cmd(&app, "rate", &rate.to_string());
}

#[tauri::command]
fn set_pitch(app: tauri::AppHandle, pitch: String) {
    send_cmd(&app, "pitch", &pitch);
}

#[tauri::command]
fn list_models() -> Vec<String> {
    let models = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("models");
    std::fs::read_dir(&models)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| {
                    let n = e.file_name().to_string_lossy().to_lowercase();
                    n.ends_with(".vrm") || n.ends_with(".glb")
                })
                .map(|e| {
                    // 返回根目录 models 下的绝对路径，供前端 asset 协议加载
                    models.join(e.file_name()).to_string_lossy().to_string()
                })
                .collect()
        })
        .unwrap_or_default()
}

/// 返回模型存放目录（根目录 models/），供前端构造 asset URL
#[tauri::command]
fn model_dir() -> String {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("models")
        .to_string_lossy()
        .to_string()
}

#[tauri::command]
fn quit(app: tauri::AppHandle) {
    app.exit(0);
}

fn main() {
    // ---- 初始化异步日志（单例，启动一次）----
    let (log_tx, log_rx) = mpsc::channel();
    let _ = LOG_TX.set(log_tx);
    let log_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("diag.log");
    thread::spawn(move || {
        let mut buffer: Vec<String> = Vec::new();
        let mut last_write = std::time::Instant::now();
        const FLUSH_INTERVAL: std::time::Duration = std::time::Duration::from_secs(2);
        const FLUSH_LEN: usize = 50;

        while let Ok(msg) = log_rx.recv() {
            buffer.push(msg);
            let now = std::time::Instant::now();
            if now.duration_since(last_write) > FLUSH_INTERVAL || buffer.len() >= FLUSH_LEN {
                if let Ok(mut f) = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&log_path)
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
        .manage(AppState(Mutex::new((AppMode::Watch, false))))
        .invoke_handler(tauri::generate_handler![
            read_text, stop_read, set_voice, set_rate, set_pitch, list_models, model_dir, quit
        ])
        .setup(|app| {
            use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut, ShortcutState};

            let log = |msg: &str| {
                log_async(format!("[{}] {}", std::process::id(), msg));
            };
            log("setup started");

            let app_handle = Arc::new(app.handle().clone());

            // ---- 主窗口：穿透常开（只开一次，永不切换）----
            let main_win = app.get_webview_window("main").expect("main window");
            if let Err(e) = main_win.set_ignore_cursor_events(true) {
                log_error(&app_handle, format!("Failed to set ignore_cursor_events: {}", e));
                log_error(&app_handle, "Falling back to normal cursor events for main window");
            } else {
                log("Main window cursor events disabled successfully");
            }

            // ---- 面板窗口：交互模式载体，可点击（不透明，否则 rgba 透明区穿透）----
            let _panel_win = tauri::WebviewWindowBuilder::new(
                app,
                "panel",
                tauri::WebviewUrl::App("panel.html".into()),
            )
                .inner_size(280.0, 400.0)
                .resizable(false)
                .decorations(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .shadow(true)
                .visible(false)
                .build()?;

            log("windows created");
            log(&format!("AppHandle cloned, Arc count: {}", Arc::strong_count(&app_handle)));

            // ---- 全局快捷键 Ctrl+Shift+T：观赏/交互切换 ----
            let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyT);
            let _ = app.global_shortcut().on_shortcut(shortcut, move |app, _s, event| {
                if event.state() != ShortcutState::Pressed {
                    return;
                }
                let app_state = app.state::<AppState>();
                let mut st = app_state.0.lock().unwrap();
                let Some(panel) = app.get_webview_window("panel") else { return };
                let Some(main) = app.get_webview_window("main") else { return };
                if st.1 {
                    let _ = panel.hide();
                    st.0 = AppMode::Watch;
                    let _ = app.emit("toggle-mode", "watch");
                    log("emit toggle-mode watch");
                    st.1 = false;
                } else {
                    if let Ok(pos) = main.outer_position() {
                        let _ = panel.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(pos.x + 260, pos.y + 20)));
                    }
                    let _ = panel.show();
                    let _ = panel.set_focus();
                    st.0 = AppMode::Interact;
                    st.1 = true;
                    let _ = app.emit("toggle-mode", "interact");
                    log("emit toggle-mode interact");
                }
            });

            // ---- Sidecar 回复 → emit tts 事件（前端 listen） ----
            static LISTENER_SIDECAR_REPLY: &str = "sidecar-reply";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_SIDECAR_REPLY) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_SIDECAR_REPLY);
                let app1 = Arc::clone(&app_handle);
                app.listen_any(LISTENER_SIDECAR_REPLY, move |ev| {
                    if let Ok(reply) = serde_json::from_str::<SidecarReply>(&ev.payload()) {
                        if reply.ok {
                            if let Some(mp3) = reply.mp3 {
                                if let Some(main) = app1.get_webview_window("main") {
                                    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                                        .join("..")
                                        .join("tts_cache")
                                        .join(&mp3);
                                    let _ = main.emit("tts", &path.to_string_lossy().to_string());
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

            // ---- 面板"返回观赏" → 隐藏面板 + 恢复模型（状态广播） ----
            static LISTENER_PANEL_CLOSING: &str = "panel-closing";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_PANEL_CLOSING) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_PANEL_CLOSING);
                let panel_app_handle = Arc::clone(&app_handle);
                app.listen_any(LISTENER_PANEL_CLOSING, move |_| {
                    log("panel-closing received");
                    let app_state = panel_app_handle.state::<AppState>();
                    let mut st = app_state.0.lock().unwrap();
                    if st.1 {
                        let _ = panel_app_handle.get_webview_window("panel").and_then(|w| w.hide().ok());
                        st.0 = AppMode::Watch;
                        let _ = panel_app_handle.emit("toggle-mode", "watch");
                        log("panel-closing: emitted watch");
                        st.1 = false;
                    }
                });
            }

            // ---- 面板就绪：若已可见则补发一次状态（对齐首次加载，不猜状态） ----
            static LISTENER_PANEL_READY: &str = "panel-ready";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_PANEL_READY) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_PANEL_READY);
                let panel_ready_app_handle = Arc::clone(&app_handle);
                app.listen_any(LISTENER_PANEL_READY, move |_| {
                    log("panel-ready received");
                    let app_state = panel_ready_app_handle.state::<AppState>();
                    let st = app_state.0.lock().unwrap();
                    if st.1 {
                        let _ = panel_ready_app_handle.emit("toggle-mode", "interact");
                        log("panel-ready: panel visible, re-emit interact");
                    }
                });
            }

            // ---- 前端确认回执（诊断用） ----
            static LISTENER_MODE_CONFIRMED: &str = "mode-confirmed";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_MODE_CONFIRMED) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_MODE_CONFIRMED);
                app.listen_any(LISTENER_MODE_CONFIRMED, move |ev| {
                    log(&format!("frontend mode-confirmed: {}", ev.payload()));
                });
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
