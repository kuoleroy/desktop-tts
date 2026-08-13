let currentText = "";
const $ = (id) => document.getElementById(id);

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

  // 朗读选中文字
  $("btn-read").addEventListener("click", () => {
    if (!currentText) return;
    window.__TAURI__.core.invoke("read_text", { text: currentText }).catch(() => {});
  });

  // 复制选中文字
  $("btn-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(currentText);
      $("btn-copy").textContent = "已复制";
      setTimeout(() => { $("btn-copy").textContent = "复制"; }, 1200);
    } catch (_) {}
  });

  // 打开设置：呼出面板（后台设置），隐藏悬浮框
  $("btn-settings").addEventListener("click", async () => {
    try {
      await window.__TAURI__.core.invoke("show_panel");
      await window.__TAURI__.window.getCurrentWindow().hide();
    } catch (_) {}
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