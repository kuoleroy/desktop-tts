// 面板窗口 TTS API 封装与通用工具
// 无 __TAURI__（纯浏览器预览）时优雅降级，不崩溃

const TTS = {
  read(text) {
    if (!window.__TAURI__?.core) return Promise.reject(new Error("预览模式无 TTS 运行时"));
    return window.__TAURI__.core.invoke("read_text", { text });
  },
  stop() {
    if (!window.__TAURI__?.core) return Promise.resolve();
    return window.__TAURI__.core.invoke("stop_read");
  },
  voice(name) {
    if (!window.__TAURI__?.core) return Promise.resolve();
    return window.__TAURI__.core.invoke("set_voice", { name });
  },
  rate(r) {
    if (!window.__TAURI__?.core) return Promise.resolve();
    return window.__TAURI__.core.invoke("set_rate", { rate: r });
  },
  pitch(p) {
    if (!window.__TAURI__?.core) return Promise.resolve();
    return window.__TAURI__.core.invoke("set_pitch", { pitch: p });
  },
  export(text) {
    if (!window.__TAURI__?.core) return Promise.reject(new Error("预览模式无 TTS 运行时"));
    return window.__TAURI__.core.invoke("export_mp3", { text });
  },
  getSettings() {
    if (!window.__TAURI__?.core) return Promise.resolve(null);
    return window.__TAURI__.core.invoke("get_settings");
  },
  providers() {
    if (!window.__TAURI__?.core) return Promise.resolve({ providers: [] });
    return window.__TAURI__.core.invoke("get_providers");
  },
  saveProviders(providers) {
    if (!window.__TAURI__?.core) return Promise.resolve(providers);
    return window.__TAURI__.core.invoke("save_providers", { providers });
  },
  fetchProviderVoices(name) {
    if (!window.__TAURI__?.core) return Promise.resolve([]);
    return window.__TAURI__.core.invoke("fetch_provider_voices", { name });
  },
  quit() {
    if (!window.__TAURI__?.core) return Promise.resolve();
    return window.__TAURI__.core.invoke("quit");
  },
};

// DOM 简写
const $ = (id) => document.getElementById(id);

// 等待 Tauri IPC 注入完成
function whenTauriReady(cb) {
  if (window.__TAURI__) return cb();
  const t = setInterval(() => {
    if (window.__TAURI__) {
      clearInterval(t);
      cb();
    }
  }, 100);
  setTimeout(() => clearInterval(t), 5000);
}

// 提示信息
function showHint(msg, isErr = false) {
  const hint = $("panel-hint");
  if (!hint) return;
  hint.textContent = msg;
  hint.style.color = isErr ? "#ff4d4f" : "#999";
  clearTimeout(showHint._t);
  showHint._t = setTimeout(() => {
    hint.textContent = "空文本时朗读默认测试句";
    hint.style.color = "#999";
  }, 4000);
}

// 获取当前文本
function currentText() {
  const t = ($("ptext") && $("ptext").value || "").trim();
  if (t) return t;
  return "你好，我是桌面小精灵。选中文字，我就能帮你朗读出来。";
}

// 设置按钮生效状态
function setActive(id, on) {
  const btn = $(id);
  if (btn) btn.classList.toggle("active", !!on);
}