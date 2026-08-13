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

/// 等待 settings 回复的通道表（id → sender）
static SETTINGS_WAITERS: std::sync::LazyLock<Mutex<std::collections::HashMap<u64, mpsc::SyncSender<SidecarReply>>>> =
    std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

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
struct GrabberState(Mutex<Option<(Child, std::sync::mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>)>>);
struct AppState(Mutex<(AppMode, bool)>);

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

/// 主窗口穿透状态（true=穿透）。Ctrl+Shift+X 切换。
/// 默认 1 = 穿透（不挡鼠标，可点击桌宠下方窗口）
static CLICK_THROUGH: AtomicU64 = AtomicU64::new(0); // 临时：默认可交互（不穿透），便于测试

#[derive(Serialize, Deserialize, Clone, Debug)]
struct SidecarReply {
    id: u64,
    ok: bool,
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mp3: Option<String>,
    /// 分块朗读时的一次合成结果列表（cmd=tts 超长文本）
    #[serde(default, skip_serializing_if = "Option::is_none")]
    files: Option<Vec<String>>,
    /// 导出 MP3 时的绝对路径（cmd=export）
    #[serde(skip_serializing_if = "Option::is_none")]
    file: Option<String>,
    /// 全局选区抓取结果（grab 消息）：true 表示带文本
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    grab: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    x: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    y: Option<i32>,
    /// 当前配置（cmd=settings）：voice/rate/pitch
    #[serde(default, skip_serializing_if = "Option::is_none")]
    settings: Option<serde_json::Value>,
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

fn spawn_sidecar(app: Arc<tauri::AppHandle>) -> Result<(Child, std::sync::mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>), String> {
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
        .current_dir(sidecar_dir())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(stderr_file))
        .spawn()
        .map_err(|e| format!("sidecar spawn failed (python={py}): {e}"))?;

    let (exit_tx, exit_rx) = std::sync::mpsc::channel::<bool>();
    let (cmd_tx, cmd_rx) = mpsc::channel::<CommandMessage>();

    // stdout 读取线程 → 解析 NDJSON → emit 到前端
    let stdout = child.stdout.take().expect("sidecar stdout");
    let app2 = Arc::clone(&app);
    thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else {
                log_error(&app2, format!("Sidecar stdout error: {line:?}"));
                break;
            };
            let Ok(reply) = serde_json::from_str::<SidecarReply>(&line) else {
                continue;
            };
            // settings 回复路由到同步等待者（get_settings 命令）
            if reply.settings.is_some() {
                let w = SETTINGS_WAITERS.lock().unwrap().remove(&reply.id);
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

    // stdin 写入线程 → 从命令队列读取 NDJSON 指令写入 sidecar stdin
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

/// 独立抓取进程脚本路径（与 TTS sidecar 隔离，避免原生崩溃拖垮朗读）
fn grabber_script() -> std::path::PathBuf {
    if cfg!(debug_assertions) {
        let dev = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("sidecar")
            .join("tts_grabber.py");
        if dev.exists() {
            return dev;
        }
    }
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("sidecar").join("tts_grabber.py")))
        .unwrap_or_else(|| std::path::PathBuf::from("sidecar/tts_grabber.py"))
}

/// 拉起独立抓取进程，读取其 stdout 中的 grab 消息并处理；返回 (child, exit_rx, cmd_tx)
fn spawn_grabber(app: Arc<tauri::AppHandle>) -> Result<(Child, std::sync::mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>), String> {
    let py = std::env::var("PYTHON").unwrap_or_else(|_| "python".into());
    let script = grabber_script();
    let err_log = sidecar_dir().join("grabber_stderr.log");
    let stderr_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&err_log)
        .map_err(|e| format!("open grabber stderr log {err_log:?}: {e}"))?;
    let mut child = Command::new(&py)
        .arg(&script)
        .current_dir(sidecar_dir())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(stderr_file))
        .spawn()
        .map_err(|e| format!("grabber spawn failed (python={py}): {e}"))?;

    // 命令通道 → 写入 grabber stdin（arm 等指令）
    let (cmd_tx, cmd_rx) = std::sync::mpsc::channel::<CommandMessage>();
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

    let (exit_tx, exit_rx) = std::sync::mpsc::channel::<bool>();
    let stdout = child.stdout.take().expect("grabber stdout");
    let app2 = Arc::clone(&app);
    thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else {
                log_error(&app2, format!("Grabber stdout error: {line:?}"));
                break;
            };
            let Ok(reply) = serde_json::from_str::<SidecarReply>(&line) else {
                continue;
            };
            if reply.grab {
                handle_grab(
                    &app2,
                    reply.text.as_deref().unwrap_or(""),
                    reply.x,
                    reply.y,
                );
            }
        }
        log_error(&app2, "Grabber process exited");
        let _ = exit_tx.send(true);
    });

    Ok((child, exit_rx, cmd_tx))
}

/// 命令抓取进程开始监控选区（点击「抓取朗读」按钮时调用）
fn grabber_cmd(app: &tauri::AppHandle, cmd: &str) {
    let state = app.state::<GrabberState>();
    let guard = state.0.lock().unwrap();
    if let Some((_child, _exit_rx, cmd_tx)) = guard.as_ref() {
        let _ = cmd_tx.send(CommandMessage { id: NEXT_ID.fetch_add(1, Ordering::Relaxed), cmd: cmd.into(), payload: String::new() });
        log_async(format!("[grabber] {cmd} requested"));
    } else {
        log_async("[grabber] not running, cannot send cmd".to_string());
    }
}

/// 前端调用：开启/关闭抓取（开关式）
#[tauri::command]
fn toggle_grab(app: tauri::AppHandle, on: bool) {
    grabber_cmd(&app, if on { "arm" } else { "disarm" });
}

/// 把目标物理位置 (x, y) 夹紧到其所在显示器的工作区内，避免面板落到屏外。
/// 返回夹紧后的 (x, y)；找不到显示器则原样返回。
fn clamp_to_work_area(
    app: &tauri::AppHandle,
    x: i32,
    y: i32,
    win_w: u32,
    win_h: u32,
) -> (i32, i32) {
    // 根据目标点找所在显示器；找不到则原样返回
    let monitor = app.monitor_from_point(x as f64, y as f64).ok().flatten();
    let Some(m) = monitor else { return (x, y) };
    let pos = m.position();
    let size = m.size();
    let margin = 12;
    // 工作区右/下边界 = 显示器位置 + 尺寸 - 面板尺寸 - 边距
    let max_x = pos.x as i32 + size.width as i32 - win_w as i32 - margin;
    let max_y = pos.y as i32 + size.height as i32 - win_h as i32 - margin;
    let cx = x.max(pos.x as i32 + margin).min(max_x.max(pos.x as i32 + margin));
    let cy = y.max(pos.y as i32 + margin).min(max_y.max(pos.y as i32 + margin));
    (cx, cy)
}

/// 处理全局选区抓取：把面板移到选区旁并填充文本，切到交互态
fn handle_grab(app: &tauri::AppHandle, text: &str, x: Option<i32>, y: Option<i32>) {
    log_async(format!("[{}] grab text ({} chars)", std::process::id(), text.chars().count()));
    // 状态：切到交互态（模型让位）；注意不要设置 st.1，否则 pet-dblclick 无法再把面板调出
    {
        let st = app.state::<AppState>();
        let mut guard = st.0.lock().unwrap();
        guard.0 = AppMode::Interact;
    }
    // 移动悬浮框到鼠标/选区位置（在鼠标下方一点，避免遮挡），并收敛到屏幕内；panel 作为后台不跟随
    if let Some(f) = app.get_webview_window("floater") {
        let win_size = f.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(200, 96));
        if let (Some(px), Some(py)) = (x, y) {
            let (cx, cy) = clamp_to_work_area(app, px + 8, py + 12, win_size.width, win_size.height);
            let _ = f.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
        }
        let _ = f.show();
    }
    // 通知悬浮框填充文本
    let _ = app.emit("floater-text", text);
    // 广播模式切换
    let _ = app.emit("toggle-mode", "interact");
}

fn send_cmd(app: &tauri::AppHandle, cmd: &str, payload: &str) {
    log_async(format!("[{}] cmd: {} {}", std::process::id(), cmd, payload));
    let app = Arc::new(app.clone());
    let state = app.state::<SidecarState>();

    // 若 sidecar 已退出，先清理再重启
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

/// 导出 MP3：sidecar 合成后写入用户 Downloads，产物路径通过 export-done 事件返回
#[tauri::command]
fn export_mp3(app: tauri::AppHandle, text: String) {
    send_cmd(&app, "export", &text);
}

#[tauri::command]
fn list_models() -> Vec<String> {
    let models = models_dir();
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

/// 返回模型存放目录（根目录 models/，规范化路径以消除 `..`），供前端构造 asset URL
fn models_dir() -> std::path::PathBuf {
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

/// 返回 TTS 音频缓存目录（规范化路径以消除 `..`，供 asset 协议访问）
fn cache_dir() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("tts_cache")
        .canonicalize()
        .unwrap_or_else(|_| {
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join("tts_cache")
        })
}

#[tauri::command]
fn model_dir() -> String {
    models_dir().to_string_lossy().to_string()
}

/// 读取当前 TTS 配置（音色/语速/语调），sidecar 可能未启动，失败返回默认
#[tauri::command]
fn get_settings(app: tauri::AppHandle) -> serde_json::Value {
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
            match spawn_sidecar(Arc::new(app.clone())) {
                Ok(s) => { *guard = Some(s); }
                Err(_) => return default,
            }
        }
        guard.as_ref().map(|(_c, _e, tx)| tx.clone())
    };
    let Some(tx) = tx else { return default };

    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let (resp_tx, resp_rx) = mpsc::sync_channel::<SidecarReply>(1);
    {
        let mut registered = SETTINGS_WAITERS.lock().unwrap();
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

/// 切换主窗口穿透/可交互（悬浮锁按钮调用）。
/// 返回切换后的穿透状态（true=穿透）。同时广播给锁窗口更新图标。
#[tauri::command]
fn toggle_click_through(app: tauri::AppHandle) -> bool {
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
fn get_click_through() -> bool {
    CLICK_THROUGH.load(Ordering::Relaxed) != 0
}

#[tauri::command]
fn quit(app: tauri::AppHandle) {
    // 关闭 sidecar 与 grabber 子进程，避免残留孤儿 python
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

fn main() {
    // ---- 崩溃捕获：panic 时同步写入 diag.log（防 abort 前丢日志）----
    {
        let log_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("diag.log");
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

    // ---- 初始化异步日志（单例，启动一次）----
    let (log_tx, log_rx) = mpsc::channel();
    let _ = LOG_TX.set(log_tx);
    let log_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("diag.log");
    thread::spawn(move || {
        use std::sync::mpsc::RecvTimeoutError;
        let mut buffer: Vec<String> = Vec::new();
        let mut last_write = std::time::Instant::now();
        const FLUSH_INTERVAL: std::time::Duration = std::time::Duration::from_secs(2);
        const FLUSH_LEN: usize = 50;

        // 用 recv_timeout 周期性唤醒，避免应用空闲时日志永远不落盘
        loop {
            let got = match log_rx.recv_timeout(std::time::Duration::from_millis(500)) {
                Ok(msg) => {
                    buffer.push(msg);
                    true
                }
                Err(RecvTimeoutError::Timeout) => false,
                Err(RecvTimeoutError::Disconnected) => break,
            };
            let now = std::time::Instant::now();
            let due = now.duration_since(last_write) >= FLUSH_INTERVAL;
            if (got && (due || buffer.len() >= FLUSH_LEN)) || (!got && due && !buffer.is_empty()) {
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
        .manage(GrabberState(Mutex::new(None)))
        .manage(AppState(Mutex::new((AppMode::Watch, false))))
        .invoke_handler(tauri::generate_handler![
            read_text, stop_read, set_voice, set_rate, set_pitch, export_mp3, list_models, model_dir, quit, toggle_grab, get_settings, toggle_click_through, get_click_through
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
            use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut, ShortcutState};

            let log = |msg: &str| {
                log_async(format!("[{}] {}", std::process::id(), msg));
            };
            log("setup started");

            let app_handle = Arc::new(app.handle().clone());

            // ---- 动态放行 asset 协议目录（config 中 $CARGO_MANIFEST_DIR 非有效变量，需运行时添加）----
            let scope = app.asset_protocol_scope();
            let _ = scope.allow_directory(models_dir(), true);
            let _ = scope.allow_directory(cache_dir(), true);

            // ---- 主窗口：按 CLICK_THROUGH 设置初始穿透状态（默认穿透，悬停自动切回交互）----
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

            // ---- 延迟复查：3 秒后再次读取主窗口状态，判断是时序问题还是创建失败 ----
            {
                let probe_app = Arc::clone(&app_handle);
                thread::spawn(move || {
                    thread::sleep(std::time::Duration::from_secs(3));
                    let win = probe_app.get_webview_window("main");
                    match win {
                        Some(w) => log_async(format!(
                            "[probe+3s] main visible={} inner={:?} outer={:?} pos={:?}",
                            w.is_visible().unwrap_or(false),
                            w.inner_size().ok(),
                            w.outer_size().ok(),
                            w.outer_position().ok()
                        )),
                        None => log_async("[probe+3s] main window NOT FOUND".into()),
                    }
                    let p = probe_app.get_webview_window("panel");
                    match p {
                        Some(w) => log_async(format!(
                            "[probe+3s] panel visible={} inner={:?}",
                            w.is_visible().unwrap_or(false),
                            w.inner_size().ok()
                        )),
                        None => log_async("[probe+3s] panel window NOT FOUND".into()),
                    }
                });
            }

            // ---- 面板窗口：交互模式载体，可点击 ----
            // 面板在 tauri.conf.json 中声明（与主窗口一致，由 Tauri 核心在 setup 前创建），
            // 这里仅取引用并记录初始化状态。实验证明：在 setup() 内程序化创建的第二个
            // WebView 窗口不会被初始化（inner=None 且页面不加载），而 config 声明的窗口正常。
            let _panel_win = app.get_webview_window("panel").expect("panel window");
            log(&format!(
                "panel init: visible={} inner={:?}",
                _panel_win.is_visible().unwrap_or(false),
                _panel_win.inner_size().ok()
            ));

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
                        let win_size = panel.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(280, 400));
                        let (cx, cy) = clamp_to_work_area(&app, pos.x + 260, pos.y + 20, win_size.width, win_size.height);
                        let _ = panel.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
                    }
                    let _ = panel.show();
                    let _ = panel.set_focus();
                    log(&format!(
                        "after panel.show(): visible={} inner={:?}",
                        panel.is_visible().unwrap_or(false),
                        panel.inner_size().ok()
                    ));
                    {
                        let pa = app.clone();
                        thread::spawn(move || {
                            thread::sleep(std::time::Duration::from_millis(1500));
                            if let Some(p) = pa.get_webview_window("panel") {
                                log_async(format!(
                                    "[panel+1.5s] visible={} inner={:?}",
                                    p.is_visible().unwrap_or(false),
                                    p.inner_size().ok()
                                ));
                            }
                        });
                    }
                    st.0 = AppMode::Interact;
                    st.1 = true;
                    let _ = app.emit("toggle-mode", "interact");
                    log("emit toggle-mode interact");
                }
            });

            // ---- 全局快捷键 Ctrl+Shift+X：切换穿透/可交互 ----
            // 穿透（不挡鼠标，可点击桌宠下方的窗口）↔ 可交互（可点击/拖动桌宠）。
            let shortcut_ct = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyX);
            let _ = app.global_shortcut().on_shortcut(shortcut_ct, move |app, _s, event| {
                if event.state() != ShortcutState::Pressed {
                    return;
                }
                let cur = CLICK_THROUGH.load(Ordering::Relaxed) != 0;
                let next = !cur;
                CLICK_THROUGH.store(next as u64, Ordering::Relaxed);
                if let Some(main) = app.get_webview_window("main") {
                    let _ = main.set_ignore_cursor_events(next);
                }
                log(&format!("Ctrl+Shift+X: click-through -> {}", next));
                let _ = app.emit("click-through-changed", next);
            });

            // ---- Sidecar 回复 → emit tts 事件（前端 listen） ----
            static LISTENER_SIDECAR_REPLY: &str = "sidecar-reply";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_SIDECAR_REPLY) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_SIDECAR_REPLY);
                let app1 = Arc::clone(&app_handle);
                app.listen_any(LISTENER_SIDECAR_REPLY, move |ev| {
                    if let Ok(reply) = serde_json::from_str::<SidecarReply>(&ev.payload()) {
                        if reply.ok {
                            // 导出 MP3：把绝对路径广播给面板
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
                            // 分块朗读：把多个文件路径依次 emit 给主窗口排队播放
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

            // ---- 双击模型 → 显示面板并切交互（与双击面板回模型互补） ----
            static LISTENER_PET_DBLCLICK: &str = "pet-dblclick";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_PET_DBLCLICK) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_PET_DBLCLICK);
                let pet_app_handle = Arc::clone(&app_handle);
                app.listen_any(LISTENER_PET_DBLCLICK, move |_| {
                    log("pet-dblclick received");
                    let app_state = pet_app_handle.state::<AppState>();
                    let mut st = app_state.0.lock().unwrap();
                    if !st.1 {
                        if let (Some(main), Some(panel)) = (
                            pet_app_handle.get_webview_window("main"),
                            pet_app_handle.get_webview_window("panel"),
                        ) {
                            if let Ok(pos) = main.outer_position() {
                                let win_size = panel.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(280, 400));
                                let (cx, cy) = clamp_to_work_area(&pet_app_handle, pos.x + 260, pos.y + 20, win_size.width, win_size.height);
                                let _ = panel.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
                            }
                            let _ = panel.show();
                        }
                        st.0 = AppMode::Interact;
                        st.1 = true;
                        let _ = pet_app_handle.emit("toggle-mode", "interact");
                        log("pet-dblclick: panel shown, interact mode");
                    }
                });
            }

            // ---- 面板就绪：WebView2 已初始化 → 默认启动即弹出面板（定位到主窗口右侧）----
            static LISTENER_PANEL_READY: &str = "panel-ready";
            if !REGISTERED_EVENTS.lock().unwrap().contains(LISTENER_PANEL_READY) {
                REGISTERED_EVENTS.lock().unwrap().insert(LISTENER_PANEL_READY);
                let panel_ready_app_handle = Arc::clone(&app_handle);
                app.listen_any(LISTENER_PANEL_READY, move |_| {
                    log("panel-ready received");
                    let app_state = panel_ready_app_handle.state::<AppState>();
                    let mut st = app_state.0.lock().unwrap();
                    if !st.1 {
                        // 默认启动即弹出：面板靠主窗口右侧显示，切到交互态
                        if let (Some(main), Some(panel)) = (
                            panel_ready_app_handle.get_webview_window("main"),
                            panel_ready_app_handle.get_webview_window("panel"),
                        ) {
                            if let Ok(pos) = main.outer_position() {
                                let win_size = panel.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(280, 400));
                                let (cx, cy) = clamp_to_work_area(&panel_ready_app_handle, pos.x + 260, pos.y + 20, win_size.width, win_size.height);
                                let _ = panel.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
                            }
                        }
                        let _ = panel_ready_app_handle.get_webview_window("panel").and_then(|p| p.show().ok());
                        st.0 = AppMode::Interact;
                        st.1 = true;
                        let _ = panel_ready_app_handle.emit("toggle-mode", "interact");
                        log("panel-ready: default show panel (interact)");
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

            // ---- 启动即拉起 TTS sidecar（朗读/导出用）----
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

            // ---- 启动即拉起独立抓取进程：让全局选区钩子从应用启动就激活 ----
            {
                let state = app_handle.state::<GrabberState>();
                let mut guard = state.0.lock().unwrap();
                if guard.is_none() {
                    match spawn_grabber(Arc::clone(&app_handle)) {
                        Ok(s) => {
                            // 启动即武装：全局任意位置选中文字即触发抓取（弹出悬浮框）
                            let _ = s.2.send(CommandMessage { id: 0, cmd: "arm".into(), payload: String::new() });
                            *guard = Some(s);
                        }
                        Err(e) => {
                            log_error(&app_handle, format!("grabber spawn failed: {e}"));
                        }
                    }
                }
            }

            // ---- watchdog：TTS sidecar 退出后自动重启，保证朗读服务稳定 ----
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
                            // 回收旧进程句柄，避免残留孤儿 python
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

            // ---- watchdog：抓取进程退出（原生崩溃）后自动重启，保证选字始终可用 ----
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
                                    // 重启后保持武装：全局选中即弹悬浮框不中断
                                    let _ = s.2.send(CommandMessage { id: 0, cmd: "arm".into(), payload: String::new() });
                                    *guard = Some(s);
                                    log_async(format!("[{}] watchdog respawned grabber (re-armed)", std::process::id()));
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
