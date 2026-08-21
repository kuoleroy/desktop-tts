// 面板主入口：事件绑定、初始化、模式切换
// 依赖 panel-tts.js, panel-voices.js, panel-settings.js

let mode = "watch";

// 尽早广播就绪
(function tryEmitReady() {
  if (window.__TAURI__?.event) {
    window.__TAURI__.event.emit("panel-ready", {}).catch(() => {});
  } else {
    setTimeout(tryEmitReady, 300);
  }
})();

whenTauriReady(() => {
  // 抓取默认开启
  setActive("p-grab", true);

  // 面板拖动 + 双击隐藏
  const panelEl = $("panel");
  if (panelEl) {
    const DRAG_THRESHOLD = 15;
    let press = null;
    let lastClick = 0, clickTimer = null;
    const isInteractive = (el) => el.closest("button, textarea, select, input, a, .prog, .toggle, label");
    panelEl.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || isInteractive(e.target)) return;
      press = { x: e.clientX, y: e.clientY, moved: false };
    });
    document.addEventListener("mousemove", (e) => {
      if (!press) return;
      const sel = window.getSelection();
      const hasSelection = sel && sel.toString().length > 0;
      if (hasSelection) { press = null; return; }
      if (!press.moved && Math.hypot(e.clientX - press.x, e.clientY - press.y) > DRAG_THRESHOLD) {
        press.moved = true;
      }
      if (press.moved) {
        press = null;
        try { window.__TAURI__.window.getCurrentWindow().startDragging(); } catch (_) {}
      }
    });
    document.addEventListener("mouseup", (e) => {
      if (press && !press.moved) {
        const now = Date.now();
        if (lastClick && now - lastClick <= 320) {
          lastClick = 0;
          window.__TAURI__.event.emit("panel-closing", {});
        } else {
          lastClick = now;
          clearTimeout(clickTimer);
          clickTimer = setTimeout(() => { lastClick = 0; }, 400);
        }
      }
      press = null;
    });
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey) press = null;
    });
  }

  // ---- Tauri 事件监听 ----
  window.__TAURI__.event.listen("toggle-mode", (e) => {
    mode = e.payload;
    document.title = "Pet Panel [" + mode + "]";
    window.__TAURI__.event.emit("mode-confirmed", "panel:" + mode);
    document.body.dataset.mode = mode;
  });

  window.__TAURI__.event.listen("export-done", (e) => {
    setActive("p-export", false);
    showHint("已导出：" + e.payload);
  });

  window.__TAURI__.event.listen("grab-text", (e) => {
    const text = (e.payload || "").toString();
    if (!$("ptext")) return;
    $("ptext").value = text;
    $("ptext").focus();
    showHint(text ? "已抓取选中文字" : "未检测到选中文字", !text);
  });

  window.__TAURI__.event.listen("play-state", (e) => {
    const s = e.payload;
    setActive("p-read", s === "playing");
    if (s === "idle") {
      setActive("p-export", false);
    }
  });

  window.__TAURI__.event.emit("panel-ready", {});

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const mgr = $("api-mgr");
    if (mgr && !mgr.classList.contains("modal-hidden")) {
      closeApiMgr();
      return;
    }
    window.__TAURI__.event.emit("panel-closing", {});
  });

  // 快捷键捕获
  document.addEventListener("keydown", (e) => {
    if (!hkCapture) return;
    e.preventDefault();
    if (e.key === "Escape") { $(hkCapture.el.id).textContent = hkFriendly($(hkCapture.el.id).dataset.orig || ""); hkCapture = null; return; }
    if (e.key === "Control" || e.key === "Shift" || e.key === "Alt" || e.key === "Meta") return;
    const mods = [];
    if (e.ctrlKey) mods.push("Ctrl");
    if (e.shiftKey) mods.push("Shift");
    if (e.altKey) mods.push("Alt");
    let key;
    if (e.code.startsWith("Key")) key = e.code.slice(3);
    else if (e.code.startsWith("Digit")) key = e.code.slice(5);
    else if (/^F\d+$/.test(e.code)) key = e.code;
    else {
      const map = { Space: "Space", Enter: "Enter", Escape: "Escape", Tab: "Tab", Backspace: "Backspace", Delete: "Delete", Insert: "Insert", Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right" };
      key = map[e.code];
    }
    if (!key) return;
    const combos = mods.concat([key]);
    if (combos.length < 2) { $(hkCapture.el.id).textContent = "需含修饰键"; return; }
    const str = combos.join("+");
    const el = hkCapture.el;
    const k = hkCapture.key;
    hkCapture = null;
    saveHk(k, str);
    el.dataset.orig = str;
    el.textContent = (k === "hotkey_panel" ? "面板:" : "穿透:") + hkFriendly(str);
    showHint("已设置" + (k === "hotkey_panel" ? "面板" : "穿透") + "快捷键：" + hkFriendly(str));
  });

  // 穿透开关
  $("p-ct").addEventListener("click", async () => {
    try {
      const next = await window.__TAURI__.core.invoke("toggle_click_through");
      setCtState(next);
      showHint(next ? "已开启穿透（不挡鼠标）" : "已关闭穿透（可交互）");
    } catch (err) {
      showHint("切换失败：" + err.message, true);
    }
  });

  window.__TAURI__.event?.listen("click-through-changed", (e) => {
    setCtState(!!e.payload);
  });

  // 启动时读取设置
  TTS.getSettings().then((s) => {
    if (!s) return;
    setGreetingUI(s);
    setMultiUI(s);
    return TTS.providers().then((list) => {
      window.__providerList = (list && list.providers) || [];
      if (s.voice) buildVoiceOptions(s.voice);
      if (s.rate != null && $("sel-rate")) $("sel-rate").value = String(s.rate);
      if (s.pitch && $("sel-pitch")) $("sel-pitch").value = s.pitch;
    });
  });

  // 快捷键绑定
  bindHkCaptureGlobal();
  // 悬浮框样式绑定
  bindFloaterStyleUI();
  // 缓存 UI 绑定
  bindCacheUI();
  // 忽略符号绑定
  bindIgnoreUI();
  // 问候语绑定
  bindGreetingUI();
  // 多开绑定
  bindMultiUI();
  // API 管理绑定
  bindApiMgr();

  // 初始设置加载
  initSettings();
});

// 朗读
$("p-read").addEventListener("click", () => {
  setActive("p-read", true);
  TTS.read(currentText()).catch((e) => {
    setActive("p-read", false);
    showHint("朗读失败：" + e.message, true);
  });
});

$("p-stop").addEventListener("click", () => {
  TTS.stop();
  if (window.__TAURI__?.event) {
    window.__TAURI__.event.emit("stop-audio", {});
  }
  if (window.__TAURI__?.core) {
    grabActive = false;
    window.__TAURI__.core.invoke("toggle_grab", { on: false }).catch(() => {});
  }
  setActive("p-read", false);
  setActive("p-export", false);
  setActive("p-grab", false);
  showHint("已停止（抓取已关闭）");
});

$("p-export").addEventListener("click", () => {
  setActive("p-export", true);
  showHint("正在合成导出...");
  TTS.export(currentText()).catch((e) => {
    setActive("p-export", false);
    showHint("导出失败：" + e.message, true);
  });
});

// 抓取开关
let grabActive = true;
$("p-grab").addEventListener("click", async () => {
  try {
    if (window.__TAURI__?.core) {
      grabActive = !grabActive;
      await window.__TAURI__.core.invoke("toggle_grab", { on: grabActive });
      setActive("p-grab", grabActive);
      showHint(grabActive ? "抓取已开启，请用鼠标选中要朗读的文字..." : "抓取已关闭");
    } else {
      const txt = await navigator.clipboard.readText();
      if (!txt || !txt.trim()) {
        showHint("剪贴板为空", true);
        return;
      }
      TTS.read(txt).catch((e) => showHint("朗读失败：" + e.message, true));
      showHint("已抓取剪贴板朗读");
    }
  } catch (e) {
    setActive("p-grab", false);
    grabActive = false;
    showHint("操作失败：" + e.message, true);
  }
});

$("sel-voice").addEventListener("change", (e) => TTS.voice(e.target.value));
$("sel-rate").addEventListener("change", (e) => TTS.rate(parseInt(e.target.value, 10)));
$("sel-pitch").addEventListener("change", (e) => TTS.pitch(e.target.value));

$("p-quit").addEventListener("click", () => {
  showHint("正在退出...");
  TTS.quit().catch((e) => showHint("退出失败：" + e.message, true));
});

const trayBtn = $("p-tray");
if (trayBtn) trayBtn.addEventListener("click", () => {
  window.__TAURI__.core.invoke("minimize_to_tray").catch(() => {});
});

// 跳过注入应用管理
async function refreshSkipList() {
  if (!window.__TAURI__?.core) { setTimeout(refreshSkipList, 1000); return; }
  try {
    const data = await window.__TAURI__.core.invoke("get_skip_apps");
    const classes = data.skip_window_classes || [];
    const exes = data.skip_exe_names || [];
    const sel = $("skip-select");
    if (!sel) return;
    const opts = [];
    classes.forEach((c) => {
      opts.push('<option value="class:' + c.replace(/"/g, "&quot;") + '">类名 ' + c + '</option>');
    });
    exes.forEach((e) => {
      opts.push('<option value="exe:' + e.replace(/"/g, "&quot;") + '">程序 ' + e + '</option>');
    });
    sel.innerHTML = opts.length ? opts.join("") : '<option value="">（暂无跳过的应用）</option>';
  } catch (err) {
    showHint("读取跳过列表失败：" + err.message, true);
  }
}

window.__TAURI__.event?.listen("skip-app-added", () => { refreshSkipList(); });

$("p-skip-pick").addEventListener("click", async () => {
  if (!window.__TAURI__?.core) return;
  showHint("请把鼠标移到要跳过的软件窗口上，等待2秒...");
  await new Promise((r) => setTimeout(r, 2000));
  try {
    const info = await window.__TAURI__.core.invoke("get_window_at");
    const cls = (info.class || "").trim();
    const exe = (info.exe || "").trim();
    if (cls === "Tauri Window" || cls === "Floater" || cls === "ttsGrabHidden" || cls === "Crop") {
      showHint("不能跳过本应用窗口", true);
      return;
    }
    if (cls) {
      await window.__TAURI__.core.invoke("add_skip_app", { appType: "class", name: cls });
      showHint("已添加：" + cls);
    } else if (exe) {
      await window.__TAURI__.core.invoke("add_skip_app", { appType: "exe", name: exe });
      showHint("已添加：" + exe);
    } else {
      showHint("未检测到有效窗口", true);
      return;
    }
    refreshSkipList();
  } catch (err) {
    showHint("选取失败：" + err.message, true);
  }
});

$("p-skip-release").addEventListener("click", async () => {
  const sel = $("skip-select");
  if (!sel) return;
  const v = sel.value;
  if (!v) { showHint("请先选择要释放的应用", true); return; }
  const i = v.indexOf(":");
  const type = v.slice(0, i) === "exe" ? "exe" : "class";
  const name = v.slice(i + 1);
  try {
    await window.__TAURI__.core.invoke("remove_skip_app", { appType: type, name });
    showHint("已释放：" + name);
    refreshSkipList();
  } catch (err) {
    showHint("释放失败：" + err.message, true);
  }
});

// 跳过抓取的应用
async function refreshGrabSkipList() {
  if (!window.__TAURI__?.core) { setTimeout(refreshGrabSkipList, 1000); return; }
  try {
    const data = await window.__TAURI__.core.invoke("get_grab_skip_apps");
    const classes = data.grab_skip_window_classes || [];
    const exes = data.grab_skip_exe_names || [];
    const sel = $("grab-skip-select");
    if (!sel) return;
    const opts = [];
    classes.forEach((c) => {
      opts.push('<option value="class:' + c.replace(/"/g, "&quot;") + '">类名 ' + c + '</option>');
    });
    exes.forEach((e) => {
      opts.push('<option value="exe:' + e.replace(/"/g, "&quot;") + '">程序 ' + e + '</option>');
    });
    sel.innerHTML = opts.length ? opts.join("") : '<option value="">（暂无跳过的应用）</option>';
  } catch (err) {
    showHint("读取抓取跳过列表失败：" + err.message, true);
  }
}

$("p-grab-skip-pick").addEventListener("click", async () => {
  if (!window.__TAURI__?.core) return;
  showHint("请把鼠标移到要跳过抓取的软件窗口上，等待2秒...");
  await new Promise((r) => setTimeout(r, 2000));
  try {
    const info = await window.__TAURI__.core.invoke("get_window_at");
    const cls = (info.class || "").trim();
    const exe = (info.exe || "").trim();
    if (cls === "Tauri Window" || cls === "Floater" || cls === "ttsGrabHidden" || cls === "Crop") {
      showHint("不能跳过本应用窗口", true);
      return;
    }
    if (cls) {
      await window.__TAURI__.core.invoke("add_grab_skip_app", { appType: "class", name: cls });
      showHint("已添加：" + cls);
    } else if (exe) {
      await window.__TAURI__.core.invoke("add_grab_skip_app", { appType: "exe", name: exe });
      showHint("已添加：" + exe);
    } else {
      showHint("未检测到有效窗口", true);
      return;
    }
    refreshGrabSkipList();
  } catch (err) {
    showHint("选取失败：" + err.message, true);
  }
});

$("p-grab-skip-release").addEventListener("click", async () => {
  const sel = $("grab-skip-select");
  if (!sel) return;
  const v = sel.value;
  if (!v) { showHint("请先选择要释放的应用", true); return; }
  const i = v.indexOf(":");
  const type = v.slice(0, i) === "exe" ? "exe" : "class";
  const name = v.slice(i + 1);
  try {
    await window.__TAURI__.core.invoke("remove_grab_skip_app", { appType: type, name });
    showHint("已释放：" + name);
    refreshGrabSkipList();
  } catch (err) {
    showHint("释放失败：" + err.message, true);
  }
});

setTimeout(refreshGrabSkipList, 800);
setTimeout(refreshSkipList, 800);

// ---- 右键菜单 ----
const ctxMenu = $("panel-ctx");
document.addEventListener("contextmenu", (e) => {
  if (e.target.closest("textarea, input, select")) return;
  e.preventDefault();
  if (!ctxMenu) return;
  const w = 130, h = 36;
  ctxMenu.style.left = Math.min(e.clientX, window.innerWidth - w - 4) + "px";
  ctxMenu.style.top = Math.min(e.clientY, window.innerHeight - h - 4) + "px";
  ctxMenu.classList.remove("ctx-hidden");
});
document.addEventListener("mousedown", (e) => {
  if (ctxMenu && e.target !== ctxMenu && !ctxMenu.contains(e.target)) {
    ctxMenu.classList.add("ctx-hidden");
  }
});
document.addEventListener("blur", () => { if (ctxMenu) ctxMenu.classList.add("ctx-hidden"); });
const ctxHide = $("ctx-hide");
if (ctxHide) {
  ctxHide.addEventListener("click", () => {
    if (ctxMenu) ctxMenu.classList.add("ctx-hidden");
    if (window.__TAURI__?.event) window.__TAURI__.event.emit("panel-closing", {});
  });
}

// ---- API 管理绑定 ----
function bindApiMgr() {
  const open = $("p-api-mgr");
  if (open) open.addEventListener("click", openApiMgr);
  const close = $("api-mgr-x");
  if (close) close.addEventListener("click", closeApiMgr);
  const mgr = $("api-mgr");
  if (mgr) mgr.querySelector(".modal-mask").addEventListener("click", closeApiMgr);
  const ft = $("f-type");
  if (ft) ft.addEventListener("change", syncTypeFields);
  const save = $("f-save");
  if (save) save.addEventListener("click", saveApiForm);
  const cancel = $("f-cancel");
  if (cancel) cancel.addEventListener("click", () => { resetApiForm(); closeApiMgr(); });
}

// 初始设置
async function initSettings() {
  if (!window.__TAURI__?.core) return;
  try {
    const s = await window.__TAURI__.core.invoke("get_app_settings");
    setCtState(!!s.click_through);
    const p = $("p-hk-panel"), c = $("p-hk-ct");
    if (p) { p.dataset.orig = s.hotkey_panel || ""; p.textContent = "面板:" + hkFriendly(s.hotkey_panel); }
    if (c) { c.dataset.orig = s.hotkey_ct || ""; c.textContent = "穿透:" + hkFriendly(s.hotkey_ct); }
    setFloaterStyleUI(s);
    setIgnoreUI(s);
    setGreetingUI(s);
  } catch (err) {
    showHint("读取设置失败：" + err.message, true);
  }
}