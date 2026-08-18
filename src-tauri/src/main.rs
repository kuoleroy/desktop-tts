#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::io::{BufRead, BufReader};
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicIsize, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use tauri::{Emitter, Listener, Manager};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers};

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
struct GrabberState(Mutex<Option<(Child, std::sync::mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>)>>);struct AppState(Mutex<(AppMode, bool)>);

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

/// 主窗口穿透状态（true=穿透）。Ctrl+Shift+X 切换。
/// 默认 1 = 穿透（不挡鼠标，可点击桌宠下方窗口）
static CLICK_THROUGH: AtomicU64 = AtomicU64::new(0); // 临时：默认可交互（不穿透），便于测试

/// 抓取总开关（初始与前端默认一致：开启）。看门狗重启 grabber 时按此状态 arm/disarm，
/// 避免用户「停止/关闭抓取」后被看门狗强制重新武装。
static GRAB_ENABLED: AtomicBool = AtomicBool::new(true);
/// 朗读锁定：非 0 时只抓取该前台窗口 hwnd，其他窗口的抓取被 grabber 忽略（避免切软件打断朗读）
static GRAB_LOCK: AtomicIsize = AtomicIsize::new(0);
/// 最近一次抓取/朗读的来源窗口 hwnd（锁定按钮据此锁定来源软件）
static GRAB_LAST_HWND: AtomicIsize = AtomicIsize::new(0);

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
    /// 抓取来源窗口句柄（朗读锁定用）
    #[serde(default, skip_serializing_if = "Option::is_none")]
    hwnd: Option<i64>,
    /// 拖放识别上报：命中跳过管理区时，grabber 上报需加入跳过注入列表的 exe 名
    #[serde(skip_serializing_if = "Option::is_none")]
    skip_exe: Option<String>,
    /// 当前配置（cmd=settings）：voice/rate/pitch
    #[serde(default, skip_serializing_if = "Option::is_none")]
    settings: Option<serde_json::Value>,
    /// 自导入音色 API 拉取结果（cmd=fetch-voices）：音色标识列表
    #[serde(default, skip_serializing_if = "Option::is_none")]
    voices: Option<Vec<String>>,
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
        // CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP：脱离终端控制台进程组。
        // 否则终端里的 Ctrl+C 会把 SIGINT 传给子进程 → Python KeyboardInterrupt 反复被杀。
        .creation_flags(0x08000200)
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
            // settings / fetch-voices 回复路由到同步等待者（get_settings / fetch_provider_voices 命令）
            if reply.settings.is_some() || reply.voices.is_some() {
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
        // CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP：脱离终端控制台进程组。
        // 否则终端里的 Ctrl+C 会把 SIGINT 传给子进程 → Python KeyboardInterrupt 反复被杀。
        .creation_flags(0x08000200)
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
                // 记录抓取来源窗口（朗读锁定用），再交给 handle_grab
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
                // 拖放识别：把识别到的软件进程加入跳过注入列表并热重载
                add_skip_app(app2.as_ref().clone(), "exe".into(), exe.clone());
                let _ = app2.emit("skip-app-added", exe);
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

/// 向抓取进程发送带负载的命令（OCR rect 等）
fn grabber_cmd_payload(app: &tauri::AppHandle, cmd: &str, payload: &str) {
    let state = app.state::<GrabberState>();
    let guard = state.0.lock().unwrap();
    if let Some((_child, _exit_rx, cmd_tx)) = guard.as_ref() {
        let _ = cmd_tx.send(CommandMessage { id: NEXT_ID.fetch_add(1, Ordering::Relaxed), cmd: cmd.into(), payload: payload.into() });
        log_async(format!("[grabber] {cmd} payload={payload}"));
    } else {
        log_async("[grabber] not running, cannot send cmd".to_string());
    }
}

/// 前端调用：对指定屏幕区域执行 OCR（全屏框选截图识别）
#[tauri::command]
fn ocr_rect(app: tauri::AppHandle, rect: String) {
    // 总闸：抓取关闭时不执行 OCR 框选识别
    if !GRAB_ENABLED.load(Ordering::Relaxed) {
        log_async("[grabber] ocr skipped (grab disabled)".to_string());
        return;
    }
    grabber_cmd_payload(&app, "ocr", &rect);
}

/// 前端调用：点按钮主动读取前台窗口选中文本（可读长文本）
#[tauri::command]
fn selread(app: tauri::AppHandle) {
    // 总闸：抓取关闭（用户「停止」）时不发起读取，避免 grabber 注入 Ctrl+C 劫持复制
    if !GRAB_ENABLED.load(Ordering::Relaxed) {
        log_async("[grabber] selread skipped (grab disabled)".to_string());
        return;
    }
    grabber_cmd(&app, "selread");
}

/// 前端调用：开启剪贴板监听窗口期（浏览器/notepad++/Edge 等无法 UIA 读取时，
/// 用户手动 Ctrl+C 复制文本，脚本据此朗读）
#[tauri::command]
fn clipwatch(app: tauri::AppHandle) {
    // 总闸：抓取关闭时不开启剪贴板监听（避免任何复制行为被它响应）
    if !GRAB_ENABLED.load(Ordering::Relaxed) {
        log_async("[grabber] clipwatch skipped (grab disabled)".to_string());
        return;
    }
    grabber_cmd(&app, "clipwatch");
}

/// 前端调用：显示全屏框选层（用户拖拽框选文字区域）
#[tauri::command]
fn show_crop(app: tauri::AppHandle) {
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

/// 前端调用：开启/关闭抓取（开关式）
#[tauri::command]
fn toggle_grab(app: tauri::AppHandle, on: bool) {
    GRAB_ENABLED.store(on, Ordering::Relaxed);
    grabber_cmd(&app, if on { "arm" } else { "disarm" });
}

/// 朗读锁定开关：锁定时记录最近抓取来源窗口 hwnd 并下发 grabber，只抓取该窗口，其他窗口忽略。
/// 返回锁定后的状态（true=已锁定）。
#[tauri::command]
fn toggle_grab_lock(app: tauri::AppHandle) -> bool {
    let cur = GRAB_LOCK.load(Ordering::Relaxed);
    if cur != 0 {
        GRAB_LOCK.store(0, Ordering::Relaxed);
        grabber_cmd_payload(&app, "grab_lock", "");
        log_async(format!("[{}] grab lock -> off", std::process::id()));
        false
    } else {
        // 锁定最近一次抓取/朗读的来源窗口（而非当前前台，因为点按钮时前台是悬浮框）
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

/// 查询朗读是否处于锁定状态
#[tauri::command]
fn get_grab_lock() -> bool {
    GRAB_LOCK.load(Ordering::Relaxed) != 0
}

/// 悬浮框「设置」→ 呼出面板作为后台设置界面（切交互态，与双击模型一致）
#[tauri::command]
fn show_panel(app: tauri::AppHandle) {
    let st = app.state::<AppState>();
    let mut guard = st.0.lock().unwrap();
    if guard.1 {
        return; // 面板已在前台，无需重复弹出
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
        // 诊断：读面板标题确认实际加载的页面（panel.js 会把路径+元素状态写入标题）
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

/// 处理全局选区抓取：显示悬浮框填充文本，隐藏面板（面板退作后台设置），返回观赏态
fn handle_grab(app: &tauri::AppHandle, text: &str, x: Option<i32>, y: Option<i32>) {
    // 总闸：抓取关闭（用户「停止」）时不弹朗读悬浮框、不处理抓取
    if !GRAB_ENABLED.load(Ordering::Relaxed) {
        return;
    }
    log_async(format!("[{}] grab text ({} chars)", std::process::id(), text.chars().count()));
    // 状态：返回观赏模式（面板隐藏、悬浮框前台）；不设置 st.1，保证 pet-dblclick 可再调出面板
    {
        let st = app.state::<AppState>();
        let mut guard = st.0.lock().unwrap();
        guard.0 = AppMode::Watch;
        guard.1 = false;
    }
    // 移动悬浮框到鼠标/选区位置（在鼠标下方一点，避免遮挡），并收敛到屏幕内
    if let Some(f) = app.get_webview_window("floater") {
        let win_size = f.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(340, 44));
        if let (Some(px), Some(py)) = (x, y) {
            let (cx, cy) = clamp_to_work_area(app, px + 8, py + 12, win_size.width, win_size.height);
            let _ = f.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(cx, cy)));
        }
        let _ = f.show();
    }
    // 隐藏面板：悬浮框作为前台快捷操作，面板退作后台设置
    let _ = app.get_webview_window("panel").and_then(|w| w.hide().ok());
    // 通知悬浮框填充文本
    let _ = app.emit("floater-text", text);
    // 同步填充面板文本区（面板隐藏时 WebView 仍在运行，下次显示即可见）
    let _ = app.emit("grab-text", text);
    // 广播模式切换（观赏：模型正常显示）
    let _ = app.emit("toggle-mode", "watch");
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
    // 音色变更联动：悬浮框等前端监听此事件实时刷新显示
    let _ = app.emit("voice-changed", name);
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

/// 返回舞蹈文件存放目录（根目录 dance/），供前端打开文件夹和 asset 协议加载用
#[tauri::command]
fn dance_dir() -> String {
    dance_root_dir().to_string_lossy().to_string()
}

/// 舞蹈文件根目录（dance/，规范化路径以消除 `..`）
fn dance_root_dir() -> std::path::PathBuf {
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

/// 列出所有可用的舞蹈动画文件（dance/ 下的 .vmd）
#[tauri::command]
fn list_dances() -> Vec<String> {
    let dance_dir = dance_root_dir();
    std::fs::read_dir(&dance_dir)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| {
                    e.file_name().to_string_lossy().to_lowercase().ends_with(".vmd")
                })
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect()
        })
        .unwrap_or_default()
}

/// 在资源管理器中打开指定目录
#[tauri::command]
fn open_folder(path: String) {
    let _ = std::process::Command::new("explorer")
        .arg(&path)
        .spawn();
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

/// 自导入音色 API 配置文件路径（根目录 voice_providers.json，与 sidecar 读取位置一致）
fn providers_file() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("voice_providers.json")
}

/// 读取自导入音色 API 配置（provider 列表），文件缺失/损坏返回空列表
#[tauri::command]
fn get_providers() -> serde_json::Value {
    std::fs::read_to_string(providers_file())
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .unwrap_or_else(|| serde_json::json!({ "providers": [] }))
}

/// 保存自导入音色 API 配置并返回保存后的值。
/// sidecar 每次合成时重新从文件读取，因此无需额外通知即可生效。
#[tauri::command]
fn save_providers(providers: serde_json::Value) -> serde_json::Value {
    if let Ok(s) = serde_json::to_string_pretty(&providers) {
        if let Some(parent) = providers_file().parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(providers_file(), s);
    }
    providers
}

/// 拉取某 provider 的可用音色列表（同步等待 sidecar 结果，最迟 35s）。
/// azure 走官方列举接口；openai/custom 返回内置默认音色。失败返回空列表。
#[tauri::command]
fn fetch_provider_voices(app: tauri::AppHandle, name: String) -> Vec<String> {
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
    if tx.send(CommandMessage { id, cmd: "fetch-voices".into(), payload: name }).is_err() {
        return default;
    }
    match resp_rx.recv_timeout(std::time::Duration::from_secs(35)) {
        Ok(r) if r.ok => r.voices.unwrap_or_default(),
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

/// 按桌面工作区比例调整模型窗口（main）大小，并居中显示。
/// scale: 1.0=全屏，0.9=9/10 ... 0.5=1/2, 0.333=1/3 等。
#[tauri::command]
fn set_main_scale(app: tauri::AppHandle, scale: f64) {
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

// ---- 应用设置：穿透状态 + 快捷键（持久化到 sidecar/settings_app.json）----

#[derive(Serialize, Deserialize, Clone, Debug)]
struct AppSettings {
    click_through: bool,
    hotkey_panel: String,
    hotkey_ct: String,
    #[serde(default = "default_floater_color")]
    floater_color: String,
    #[serde(default = "default_floater_opacity")]
    floater_opacity: f64,
    /// 朗读时忽略成对符号包裹的内容（如 [注]、【】、（）等）
    #[serde(default = "default_ignore_pairs")]
    ignore_pairs: bool,
    /// 用户自定义的忽略符号对列表，每项形如 "[]"、"【】"
    #[serde(default = "default_ignore_symbols")]
    ignore_symbols: Vec<String>,
}

fn default_floater_color() -> String { "#1e2026".into() }
fn default_floater_opacity() -> f64 { 0.84 }
fn default_ignore_pairs() -> bool { true }
fn default_ignore_symbols() -> Vec<String> {
    vec!["[]".into(), "{}".into(), "【】".into(), "（）".into(), "()".into(), "《》".into(), "<>".into()]
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            click_through: false,
            hotkey_panel: "Ctrl+Shift+T".into(),
            hotkey_ct: "Ctrl+Shift+X".into(),
            floater_color: default_floater_color(),
            floater_opacity: default_floater_opacity(),
            ignore_pairs: default_ignore_pairs(),
            ignore_symbols: default_ignore_symbols(),
        }
    }
}

fn app_settings_path() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("sidecar")
        .join("settings_app.json")
}

fn load_app_settings() -> AppSettings {
    std::fs::read_to_string(app_settings_path())
        .ok()
        .and_then(|s| serde_json::from_str::<AppSettings>(&s).ok())
        .unwrap_or_default()
}

fn save_app_settings(s: &AppSettings) {
    if let Ok(json) = serde_json::to_string_pretty(s) {
        let _ = std::fs::write(app_settings_path(), json);
    }
}

/// 解析形如 "Ctrl+Shift+X" 的快捷键字符串 → (Modifiers, Code)。返回 None 表示无法解析。
fn parse_shortcut(s: &str) -> Option<(Modifiers, Code)> {
    let parts: Vec<&str> = s.split('+').map(|p| p.trim()).collect();
    let (key, mods) = parts.split_last()?;
    let mut m = Modifiers::empty();
    for p in mods {
        m |= match p.to_lowercase().as_str() {
            "ctrl" | "control" => Modifiers::CONTROL,
            "shift" => Modifiers::SHIFT,
            "alt" => Modifiers::ALT,
            "super" | "win" | "cmd" => Modifiers::SUPER,
            _ => return None,
        };
    }
    let code = parse_code(key.trim())?;
    Some((m, code))
}

fn parse_code(k: &str) -> Option<Code> {
    use Code::*;
    if k.len() == 1 && k.as_bytes()[0].is_ascii_alphabetic() {
        let c = k.to_ascii_uppercase();
        return Some(match c.as_str() {
            "A" => KeyA, "B" => KeyB, "C" => KeyC, "D" => KeyD, "E" => KeyE, "F" => KeyF,
            "G" => KeyG, "H" => KeyH, "I" => KeyI, "J" => KeyJ, "K" => KeyK, "L" => KeyL,
            "M" => KeyM, "N" => KeyN, "O" => KeyO, "P" => KeyP, "Q" => KeyQ, "R" => KeyR,
            "S" => KeyS, "T" => KeyT, "U" => KeyU, "V" => KeyV, "W" => KeyW, "X" => KeyX,
            "Y" => KeyY, "Z" => KeyZ, _ => return None,
        });
    }
    Some(match k.to_lowercase().as_str() {
        "space" => Space, "enter" => Enter, "escape" | "esc" => Escape, "tab" => Tab,
        "backspace" => Backspace, "delete" => Delete, "insert" => Insert, "home" => Home,
        "end" => End, "pageup" => PageUp, "pagedown" => PageDown,
        "f1" => F1, "f2" => F2, "f3" => F3, "f4" => F4, "f5" => F5, "f6" => F6,
        "f7" => F7, "f8" => F8, "f9" => F9, "f10" => F10, "f11" => F11, "f12" => F12,
        "f13" => F13, "f14" => F14, "f15" => F15, "f16" => F16,
        "up" => ArrowUp, "down" => ArrowDown, "left" => ArrowLeft, "right" => ArrowRight,
        "0" => Digit0, "1" => Digit1, "2" => Digit2, "3" => Digit3, "4" => Digit4,
        "5" => Digit5, "6" => Digit6, "7" => Digit7, "8" => Digit8, "9" => Digit9,
        _ => return None,
    })
}

/// 面板显示/隐藏切换（观赏 ↔ 交互）
/// 面板显示/隐藏切换（右键菜单用）
#[tauri::command]
fn toggle_panel_ui(app: tauri::AppHandle) {
    toggle_panel(&app);
}

/// 查询面板当前是否在前台（右键菜单据此显示「显示面板/隐藏面板」）
#[tauri::command]
fn get_panel_visible(app: tauri::AppHandle) -> bool {
    let st = app.state::<AppState>();
    let guard = st.0.lock().unwrap();
    guard.1
}

fn toggle_panel(app: &tauri::AppHandle) {
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

/// 穿透/可交互切换（true=穿透，不挡鼠标）
fn toggle_ct(app: &tauri::AppHandle) {
    let cur = CLICK_THROUGH.load(Ordering::Relaxed) != 0;
    let next = !cur;
    CLICK_THROUGH.store(next as u64, Ordering::Relaxed);
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.set_ignore_cursor_events(next);
    }
    log_async(format!("hotkey: click-through -> {}", next));
    let _ = app.emit("click-through-changed", next);
}

/// 应用设置中的两个快捷键：先注销全部再按配置注册。
fn apply_hotkeys(app: &tauri::AppHandle, panel_shortcut: &str, ct_shortcut: &str) {
    let _ = app.global_shortcut().unregister_all();
    use tauri_plugin_global_shortcut::{Shortcut, ShortcutState};
    if let Some((m, c)) = parse_shortcut(panel_shortcut) {
        let shortcut = Shortcut::new(Some(m), c);
        let _ = app.global_shortcut().on_shortcut(shortcut, move |app, _s, event| {
            if event.state() != ShortcutState::Pressed { return; }
            toggle_panel(&app);
        });
        log_async(format!("hotkey panel registered: {}", panel_shortcut));
    } else {
        log_async(format!("hotkey panel invalid: {}", panel_shortcut));
    }
    if let Some((m, c)) = parse_shortcut(ct_shortcut) {
        let shortcut = Shortcut::new(Some(m), c);
        let _ = app.global_shortcut().on_shortcut(shortcut, move |app, _s, event| {
            if event.state() != ShortcutState::Pressed { return; }
            toggle_ct(&app);
        });
        log_async(format!("hotkey ct registered: {}", ct_shortcut));
    } else {
        log_async(format!("hotkey ct invalid: {}", ct_shortcut));
    }
}

#[tauri::command]
fn get_app_settings() -> AppSettings {
    load_app_settings()
}

#[tauri::command]
fn set_app_settings(app: tauri::AppHandle, settings: AppSettings) -> Result<(), String> {
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

// ---- 跳过注入应用管理（skip_apps.json）----

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
struct SkipConfig {
    skip_window_classes: Vec<String>,
    skip_exe_names: Vec<String>,
}

fn skip_config_path() -> std::path::PathBuf {
    sidecar_dir().join("skip_apps.json")
}

fn read_skip_config() -> SkipConfig {
    let path = skip_config_path();
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn write_skip_config(config: &SkipConfig) {
    let path = skip_config_path();
    if let Ok(json) = serde_json::to_string_pretty(config) {
        let _ = std::fs::write(&path, &json);
    }
}

/// 通知 grabber 重载跳过配置
fn notify_grabber_reload_skip(app: &tauri::AppHandle) {
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

#[tauri::command]
fn get_skip_apps() -> SkipConfig {
    read_skip_config()
}

#[tauri::command]
fn add_skip_app(app: tauri::AppHandle, app_type: String, name: String) {
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
fn remove_skip_app(app: tauri::AppHandle, app_type: String, name: String) {
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
fn clear_skip_apps(app: tauri::AppHandle) {
    write_skip_config(&SkipConfig::default());
    notify_grabber_reload_skip(&app);
}

// ---- 跳过抓取应用管理（grab_skip_apps.json）：仅影响抓取读取，不影响注入 ----

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
struct GrabSkipConfig {
    grab_skip_window_classes: Vec<String>,
    grab_skip_exe_names: Vec<String>,
}

fn grab_skip_config_path() -> std::path::PathBuf {
    sidecar_dir().join("grab_skip_apps.json")
}

fn read_grab_skip_config() -> GrabSkipConfig {
    let path = grab_skip_config_path();
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn write_grab_skip_config(config: &GrabSkipConfig) {
    let path = grab_skip_config_path();
    if let Ok(json) = serde_json::to_string_pretty(config) {
        let _ = std::fs::write(&path, &json);
    }
}

#[tauri::command]
fn get_grab_skip_apps() -> GrabSkipConfig {
    read_grab_skip_config()
}

#[tauri::command]
fn add_grab_skip_app(app: tauri::AppHandle, app_type: String, name: String) {
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
fn remove_grab_skip_app(app: tauri::AppHandle, app_type: String, name: String) {
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
fn clear_grab_skip_apps(app: tauri::AppHandle) {
    write_grab_skip_config(&GrabSkipConfig::default());
    notify_grabber_reload_skip(&app);
}

#[tauri::command]
fn get_fg_window_info() -> serde_json::Value {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::OpenProcess;
    use windows_sys::Win32::UI::WindowsAndMessaging::{GetClassNameW, GetForegroundWindow, GetWindowThreadProcessId};

    let mut class = String::new();
    let mut exe = String::new();

    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd != 0 {
            // 获取窗口类名
            let mut buf = [0u16; 256];
            let len = GetClassNameW(hwnd, buf.as_mut_ptr(), 256);
            if len > 0 {
                class = String::from_utf16_lossy(&buf[..len as usize]);
            }

            // 获取进程 exe 名
            let mut pid: u32 = 0;
            let _ = GetWindowThreadProcessId(hwnd, &mut pid);
            if pid != 0 {
                let h = OpenProcess(0x1000, 0, pid); // PROCESS_QUERY_LIMITED_INFORMATION
                if h != 0 {
                    let mut exe_buf = [0u16; 1024];
                    let mut size = 1024u32;
                    if windows_sys::Win32::System::Threading::QueryFullProcessImageNameW(h, 0, exe_buf.as_mut_ptr(), &mut size) != 0 {
                        let path = String::from_utf16_lossy(&exe_buf[..size as usize]);
                        // 只取文件名
                        if let Some(base) = std::path::Path::new(&path).file_name() {
                            exe = base.to_string_lossy().to_lowercase();
                        }
                    }
                    CloseHandle(h);
                }
            }
        }
    }

    serde_json::json!({
        "class": class,
        "exe": exe
    })
}

/// 读取鼠标当前位置下的顶层窗口（进程）。不受置顶面板抢前台影响，
/// 用于「跳过注入」的鼠标位置识别：把鼠标移到目标软件上 → 识别其窗口进程。
#[tauri::command]
fn get_window_at() -> serde_json::Value {
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
            hwnd = GetAncestor(w, 2) as usize; // GA_ROOT = 2 取顶层窗口
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

    serde_json::json!({
        "class": class,
        "exe": exe
    })
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
            read_text, stop_read, set_voice, set_rate, set_pitch, export_mp3, list_models, model_dir, quit, toggle_grab, toggle_grab_lock, get_grab_lock, show_panel, get_settings, toggle_click_through, get_click_through, ocr_rect, show_crop, selread, clipwatch,
            get_skip_apps, add_skip_app, remove_skip_app, clear_skip_apps, get_fg_window_info, get_window_at,
            get_grab_skip_apps, add_grab_skip_app, remove_grab_skip_app, clear_grab_skip_apps,
            get_app_settings, set_app_settings, get_click_through, toggle_click_through,
            set_main_scale, toggle_panel_ui, get_panel_visible,
            list_dances, open_folder, dance_dir,
            get_providers, save_providers, fetch_provider_voices
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

            let app_handle = Arc::new(app.handle().clone());

            // ---- 动态放行 asset 协议目录（config 中 $CARGO_MANIFEST_DIR 非有效变量，需运行时添加）----
            let scope = app.asset_protocol_scope();
            let _ = scope.allow_directory(models_dir(), true);
            let _ = scope.allow_directory(cache_dir(), true);
            let _ = scope.allow_directory(dance_root_dir(), true);

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

            // ---- 全屏框选层：点击「框选截图」时全屏显示，用户拖拽框选区域 ----
            if let Some(crop_win) = app.get_webview_window("crop") {
                let _ = crop_win.set_fullscreen(true);
                let _ = crop_win.hide();
                log("crop window initialized (fullscreen, hidden)");
            }

log("windows created");
            log(&format!("AppHandle cloned, Arc count: {}", Arc::strong_count(&app_handle)));

            // ---- 全局快捷键（可配置）：面板显示/隐藏 + 穿透切换 ----
            // 默认 Ctrl+Shift+T（面板）、Ctrl+Shift+X（穿透），可用 set_app_settings 修改。
            {
                let settings = load_app_settings();
                CLICK_THROUGH.store(if settings.click_through { 1 } else { 0 }, Ordering::Relaxed);
                if let Some(main) = app.get_webview_window("main") {
                    let _ = main.set_ignore_cursor_events(settings.click_through);
                }
apply_hotkeys(app.handle(), &settings.hotkey_panel, &settings.hotkey_ct);
            }

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
                    // 用户按 Esc / 双击面板 → 无条件隐藏面板与悬浮框。
                    // 不依赖 st.1：该标志可能因 panel-ready 竞态与面板可见性失配，
                    // 若在此拦截会导致面板永远无法隐藏。
                    let _ = panel_app_handle.get_webview_window("panel").and_then(|w| w.hide().ok());
                    let _ = panel_app_handle.get_webview_window("floater").and_then(|w| w.hide().ok());
                    st.0 = AppMode::Watch;
                    st.1 = false;
                    let _ = panel_app_handle.emit("toggle-mode", "watch");
                    log("panel-closing: emitted watch");
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
                                let win_size = panel.inner_size().ok().unwrap_or(tauri::PhysicalSize::new(420, 600));
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
                            // 记事本等应用事件稳定好用；浏览器/opencode 偶发误触在下方过滤
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
                                    // 重启后按用户设置的抓取开关决定 arm/disarm：
                                    // 开启则武装（保持"选中即弹悬浮框"），关闭则保持停止
                                    let cmd = if GRAB_ENABLED.load(Ordering::Relaxed) { "arm" } else { "disarm" };
                                    let _ = s.2.send(CommandMessage { id: 0, cmd: cmd.into(), payload: String::new() });
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
