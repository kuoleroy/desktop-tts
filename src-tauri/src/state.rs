use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, AtomicIsize, AtomicU64};
use std::sync::{mpsc, Arc, Mutex};

pub static REGISTERED_EVENTS: std::sync::LazyLock<Mutex<HashSet<&'static str>>> =
    std::sync::LazyLock::new(|| Mutex::new(HashSet::new()));

pub static LOG_TX: std::sync::OnceLock<mpsc::Sender<String>> = std::sync::OnceLock::new();

pub static SETTINGS_WAITERS: std::sync::LazyLock<
    Mutex<std::collections::HashMap<u64, mpsc::SyncSender<SidecarReply>>>,
> = std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

pub fn log_async(msg: String) {
    if let Some(tx) = LOG_TX.get() {
        let _ = tx.send(msg);
    }
}

pub fn log_error(_app: &Arc<tauri::AppHandle>, msg: impl AsRef<str>) {
    log_async(format!("[{}] ERROR: {}", std::process::id(), msg.as_ref()));
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AppMode {
    Watch,
    Interact,
}

#[derive(Clone)]
pub struct CommandMessage {
    pub id: u64,
    pub cmd: String,
    pub payload: String,
}

pub struct SidecarState(pub Mutex<Option<(std::process::Child, mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>)>>);
pub struct GrabberState(pub Mutex<Option<(std::process::Child, mpsc::Receiver<bool>, mpsc::Sender<CommandMessage>)>>);
pub struct AppState(pub Mutex<(AppMode, bool)>);

pub static NEXT_ID: AtomicU64 = AtomicU64::new(1);

pub static CLICK_THROUGH: AtomicU64 = AtomicU64::new(0);

pub static GRAB_ENABLED: AtomicBool = AtomicBool::new(true);

pub static LAST_GRAB: std::sync::LazyLock<Mutex<std::time::Instant>> = std::sync::LazyLock::new(|| {
    Mutex::new(std::time::Instant::now() - std::time::Duration::from_secs(10))
});

pub static GRAB_LOCK: AtomicIsize = AtomicIsize::new(0);
pub static GRAB_LAST_HWND: AtomicIsize = AtomicIsize::new(0);

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct SidecarReply {
    pub id: u64,
    pub ok: bool,
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mp3: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub files: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file: Option<String>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub grab: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub x: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub y: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hwnd: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub skip_exe: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub settings: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub voices: Option<Vec<String>>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct AppSettings {
    pub click_through: bool,
    pub hotkey_panel: String,
    pub hotkey_ct: String,
    #[serde(default = "default_floater_color")]
    pub floater_color: String,
    #[serde(default = "default_floater_opacity")]
    pub floater_opacity: f64,
    #[serde(default = "default_ignore_pairs")]
    pub ignore_pairs: bool,
    #[serde(default = "default_ignore_symbols")]
    pub ignore_symbols: Vec<String>,
    #[serde(default = "default_strip_symbols")]
    pub strip_symbols: bool,
    #[serde(default = "default_strip_symbol_chars")]
    pub strip_symbol_chars: String,
    #[serde(default = "default_greeting")]
    pub greeting: String,
    #[serde(default = "default_cache_limit_mb")]
    pub cache_limit_mb: u64,
    #[serde(default = "default_multi_instance")]
    pub multi_instance: bool,
}

fn default_floater_color() -> String { "#1e2026".into() }
fn default_floater_opacity() -> f64 { 0.84 }
fn default_ignore_pairs() -> bool { true }
fn default_ignore_symbols() -> Vec<String> {
    vec!["[]".into(), "{}".into(), "【】".into(), "（）".into(), "()".into(), "《》".into(), "<>".into()]
}
fn default_strip_symbols() -> bool { true }
fn default_strip_symbol_chars() -> String { "*~`#>|_-".into() }
fn default_greeting() -> String { "你好，我是桌面小精灵，欢迎回来！".into() }
fn default_cache_limit_mb() -> u64 { 500 }
fn default_multi_instance() -> bool { true }

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
            strip_symbols: default_strip_symbols(),
            strip_symbol_chars: default_strip_symbol_chars(),
            greeting: default_greeting(),
            cache_limit_mb: default_cache_limit_mb(),
            multi_instance: default_multi_instance(),
        }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct SkipConfig {
    pub skip_window_classes: Vec<String>,
    pub skip_exe_names: Vec<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct GrabSkipConfig {
    pub grab_skip_window_classes: Vec<String>,
    pub grab_skip_exe_names: Vec<String>,
}