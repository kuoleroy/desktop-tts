let currentText = "";
const $ = (id) => document.getElementById(id);

// 悬浮框背景：颜色(hex) + 透明度(0-1) → 应用到 body::before 引用的 --floater-bg
function applyFloaterStyle(color, opacity) {
  const c = (/^#[0-9a-fA-F]{6}$/.test(color || "")) ? color : "#1e2026";
  const a = (typeof opacity === "number" && opacity >= 0 && opacity <= 1) ? opacity : 0.84;
  const n = parseInt(c.slice(1), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  document.body.style.setProperty("--floater-bg", "rgba(" + r + "," + g + "," + b + "," + a + ")");
}

// 音色值 → 友好名称（与面板下拉框一致）
const VOICE_NAMES = {
  "zh-CN-XiaoxiaoNeural": "晓晓·自然女声",
  "zh-CN-YunxiNeural": "云希·阳光男声",
  "zh-CN-YunyangNeural": "云扬·沉稳男声",
  "zh-CN-XiaoyiNeural": "晓伊·活泼女声",
  "zh-CN-YunjianNeural": "云健·运动男声",
  "local:Microsoft Xiaoxiao (Natural)": "晓晓·本地自然音",
  "local:Microsoft Yunxi (Natural)": "云希·本地自然音",
};
function voiceLabel(name) {
  if (!name) return "-";
  return VOICE_NAMES[name] || name;
}
function setVoice(name) {
  const el = $("voice");
  if (el) el.textContent = voiceLabel(name);
}

function whenTauriReady(cb) {
  if (window.__TAURI__?.event) return cb();
  const t = setInterval(() => {
    if (window.__TAURI__?.event) { clearInterval(t); cb(); }
  }, 100);
  setTimeout(() => clearInterval(t), 5000);
}

whenTauriReady(() => {
  // 抓到选中文字 → 填充并更新字数
  window.__TAURI__.event.listen("floater-text", (e) => {
    currentText = String(e.payload || "");
    $("count").textContent = currentText.length;
  });

  // 面板改音色 → 悬浮框实时刷新当前音色（联动）
  window.__TAURI__.event.listen("voice-changed", (e) => {
    setVoice(String(e.payload || ""));
  });

  // 面板改悬浮框背景（颜色/透明度）→ 实时应用
  window.__TAURI__.event.listen("floater-style-changed", (e) => {
    const p = e.payload || {};
    applyFloaterStyle(p.color, p.opacity);
  });

  // 启动时按持久化设置应用悬浮框背景
  window.__TAURI__.core.invoke("get_app_settings").then((s) => {
    if (s) applyFloaterStyle(s.floater_color, s.floater_opacity);
  }).catch(() => {});

  // 朗读选中文字：点「朗读」→ 主动读取前台窗口选中文本(可读长文本)→ 朗读。
  // 若应用无法 UIA 读取(浏览器/notepad++/Edge)，同步开启剪贴板监听窗口期，
  // 用户在几秒内手动 Ctrl+C 复制文本，脚本据此朗读。
  $("btn-read").addEventListener("click", async () => {
    try {
      await window.__TAURI__.core.invoke("selread");
      // 同步开启剪贴板监听：UIA 读不到时兜底读用户复制的文本
      await window.__TAURI__.core.invoke("clipwatch").catch(() => {});
      // 抓取结果通过 floater-text 事件回填，短暂等待后朗读
      await new Promise(r => setTimeout(r, 400));
      if (currentText) {
        await window.__TAURI__.core.invoke("read_text", { text: currentText }).catch(() => {});
      }
    } catch (_) {}
  });

  // 复制选中文字
  $("btn-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(currentText);
    } catch (_) {}
  });

  // 暂停/继续播放
  $("btn-pause").addEventListener("click", async () => {
    const btn = $("btn-pause");
    const isPaused = btn.classList.contains("active");
    if (window.__TAURI__?.event) {
      await window.__TAURI__.event.emit(isPaused ? "resume-audio" : "pause-audio", {});
    }
    btn.classList.toggle("active");
    $("pause-icon").style.display = isPaused ? "" : "none";
    $("play-icon").style.display = isPaused ? "none" : "";
    btn.title = isPaused ? "暂停" : "继续";
  });

  // 锁定当前软件：锁定后 grabber 只抓取当前前台窗口，切到其他软件不捕捉，朗读不被打断
  function setLockState(on) {
    const btn = $("btn-lock");
    btn.classList.toggle("active", on);
    btn.title = on ? "已锁定当前软件（点击解锁）" : "锁定当前软件（不捕捉其他软件）";
  }
  window.__TAURI__.core.invoke("get_grab_lock").then(setLockState).catch(() => {});
  $("btn-lock").addEventListener("click", async () => {
    try {
      const locked = await window.__TAURI__.core.invoke("toggle_grab_lock");
      setLockState(locked);
    } catch (_) {}
  });

  // 打开设置：呼出面板（后台设置），隐藏悬浮框
  $("btn-settings").addEventListener("click", async () => {
    try {
      await window.__TAURI__.core.invoke("show_panel");
      await window.__TAURI__.window.getCurrentWindow().hide();
    } catch (_) {}
  });

  // 框选层显示时隐藏本窗口（兜底）
  window.__TAURI__.event.listen("hide-floater", () => {
    try { window.__TAURI__.window.getCurrentWindow().hide(); } catch (_) {}
  });

  // 启动时读取当前音色显示
  window.__TAURI__.core.invoke("get_settings").then((s) => {
    if (s && s.voice) setVoice(s.voice);
  }).catch(() => {});

  // Esc 关闭悬浮框与面板（返回观赏）
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") window.__TAURI__.event.emit("panel-closing", {});
  });

  // 悬浮框可拖动（按住空白拖动）
  const DRAG_THRESHOLD = 6;
  let press = null;
  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest("button")) return;
    press = { x: e.clientX, y: e.clientY, moved: false };
  });
  document.addEventListener("mousemove", (e) => {
    if (!press) return;
    if (!press.moved && Math.hypot(e.clientX - press.x, e.clientY - press.y) > DRAG_THRESHOLD) {
      press.moved = true;
    }
    if (press.moved) {
      press = null;
      try { window.__TAURI__.window.getCurrentWindow().startDragging(); } catch (_) {}
    }
  });
  document.addEventListener("mouseup", () => { press = null; });
});