// 面板窗口：交互模式载体（不穿透，可点击）
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

const $ = (id) => document.getElementById(id);
let mode = "watch";

// ---- 音色下拉：内置(微软在线/本地离线) + 自导入音色 API 分组 ----
const EDGE_VOICES = [
  ["zh-CN-XiaoxiaoNeural", "晓晓（自然女声·在线）"],
  ["zh-CN-YunxiNeural", "云希（阳光男声·在线）"],
  ["zh-CN-YunyangNeural", "云扬（沉稳男声·在线）"],
  ["zh-CN-XiaoyiNeural", "晓伊（活泼女声·在线）"],
  ["zh-CN-YunjianNeural", "云健（运动男声·在线）"],
];
const LOCAL_VOICES = [
  ["local:Microsoft Xiaoxiao (Natural)", "晓晓·本地自然音（离线）"],
  ["local:Microsoft Yunxi (Natural)", "云希·本地自然音（离线）"],
];
// OpenAI 兼容类型无「列举音色」接口时的内置兜底（Azure 走官方列举接口）
const OPENAI_DEF_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"];

function providerVoices(p) {
  if (p.voices && p.voices.length) return p.voices;
  if (p.type === "azure") return [];
  return OPENAI_DEF_VOICES;
}

function _opt(v, text, disabled) {
  const o = document.createElement("option");
  o.value = v;
  o.textContent = text;
  if (disabled) o.disabled = true;
  return o;
}

function _safeName(name) { return String(name).replace(/:/g, "·"); }

// 重建音色下拉：先内置，再追加每个自导入 API 的分组
function buildVoiceOptions(current) {
  const sel = $("sel-voice");
  if (!sel) return;
  sel.innerHTML = "";
  const ogOn = document.createElement("optgroup");
  ogOn.label = "微软在线（edge-tts）";
  EDGE_VOICES.forEach(([v, t]) => ogOn.appendChild(_opt(v, t)));
  sel.appendChild(ogOn);
  const ogLo = document.createElement("optgroup");
  ogLo.label = "本地离线（SAPI）";
  LOCAL_VOICES.forEach(([v, t]) => ogLo.appendChild(_opt(v, t)));
  sel.appendChild(ogLo);
  (window.__providerList || []).forEach((p) => {
    const og = document.createElement("optgroup");
    og.label = _safeName(p.name) + (p.type === "azure" ? " · Azure" : " · API");
    const list = providerVoices(p);
    if (list.length) {
      list.forEach((pv) => og.appendChild(_opt("api:" + _safeName(p.name) + ":" + pv, pv)));
    } else {
      og.appendChild(_opt("", "（音色未拉取，请先拉取音色）", true));
    }
    sel.appendChild(og);
  });
  if (current) sel.value = current;
}

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

function currentText() {
  const t = ($("ptext") && $("ptext").value || "").trim();
  if (t) return t;
  return "你好，我是桌面小精灵。选中文字，我就能帮你朗读出来。";
}

// 设置某个按钮的生效(蓝色)状态
function setActive(id, on) {
  const btn = $(id);
  if (btn) btn.classList.toggle("active", !!on);
}

// 状态同步：Rust 全局广播 toggle-mode，面板只接收、不猜状态
// Windows 上 __TAURI__ 注入晚于顶层脚本（tauri #12990），须等就绪
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

// 尽早广播就绪（Rust 侧据此显示面板并切交互模式）。
// 不依赖 whenTauriReady：__TAURI__ 注入慢时其 5 秒轮询会超时，导致面板一直停留穿透态。
(function tryEmitReady() {
  if (window.__TAURI__?.event) {
    window.__TAURI__.event.emit("panel-ready", {}).catch(() => {});
  } else {
    setTimeout(tryEmitReady, 300);
  }
})();

whenTauriReady(() => {
  // 抓取默认开启（启动即 arm，全局选中即弹悬浮框）→ 按钮初始显示蓝色
  setActive("p-grab", true);

  // 整个面板可拖动 + 双击隐藏回模型
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
      // 若产生了文本选区（正在拖选文字/复制），不触发面板拖动
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
    // Ctrl 组合（如 Ctrl+C 复制）时不触发面板拖动
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey) press = null;
    });
  }

  window.__TAURI__.event.listen("toggle-mode", (e) => {
    mode = e.payload;
    document.title = "Pet Panel [" + mode + "]";
    window.__TAURI__.event.emit("mode-confirmed", "panel:" + mode);
    document.body.dataset.mode = mode;
  });
  // 导出完成：Rust 广播绝对路径
  window.__TAURI__.event.listen("export-done", (e) => {
    setActive("p-export", false);
    showHint("已导出：" + e.payload);
  });

  // 全局选区抓取：sidecar 捕获选中文字后，Rust 广播 grab-text 填充文本框
  // 抓取是开关式，抓到后保持开启以便连续抓取
  window.__TAURI__.event.listen("grab-text", (e) => {
    const text = (e.payload || "").toString();
    if (!$("ptext")) return;
    $("ptext").value = text;
    $("ptext").focus();
    showHint(text ? "已抓取选中文字" : "未检测到选中文字", !text);
  });

  // 播放状态：主窗口广播 playing/paused/idle，同步朗读与暂停按钮
  window.__TAURI__.event.listen("play-state", (e) => {
    const s = e.payload;
    setActive("p-read", s === "playing");
    if (s === "idle") {
      setActive("p-export", false);
    }
  });

  // 朗读进度：主窗口广播 frac(0-1) + 当前块内秒数，驱动进度条
  // （进度条暂不需要，逻辑以注释保留以防误删）
  /*
  window.__TAURI__.event.listen("read-progress", (e) => {
    const p = e.payload || {};
    const frac = typeof p.frac === "number" ? p.frac : 0;
    const wrap = $("prog-wrap");
    const fill = $("prog-fill");
    const label = $("prog-label");
    if (!wrap || !fill || !label) return;
    if (frac <= 0) {
      wrap.classList.add("hidden");
      return;
    }
    wrap.classList.remove("hidden");
    fill.style.width = Math.round(frac * 100) + "%";
    label.textContent = Math.round(frac * 100) + "%";
  });
  */

  // 进入面板时广播就绪（Rust 侧可据此补发状态）
  window.__TAURI__.event.emit("panel-ready", {});

  // Esc 关闭面板（若管理音色 API 弹窗打开，则优先只关闭弹窗）
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const mgr = $("api-mgr");
    if (mgr && !mgr.classList.contains("modal-hidden")) {
      closeApiMgr();
      return;
    }
    window.__TAURI__.event.emit("panel-closing", {});
  });

  // 启动时读取持久化配置并同步下拉框（音色/语速/语调）
  TTS.getSettings().then((s) => {
    if (!s) return;
    return TTS.providers().then((list) => {
      window.__providerList = (list && list.providers) || [];
      if (s.voice) buildVoiceOptions(s.voice);
      if (s.rate != null && $("sel-rate")) $("sel-rate").value = String(s.rate);
      if (s.pitch && $("sel-pitch")) $("sel-pitch").value = s.pitch;
    });
  });
});

// 朗读：读文本区内容（空则用默认句），播放中按钮变蓝
$("p-read").addEventListener("click", () => {
  setActive("p-read", true);
  TTS.read(currentText()).catch((e) => {
    setActive("p-read", false);
    showHint("朗读失败：" + e.message, true);
  });
});

// 停止：广播给主窗口真实停止音频，同时通知 sidecar；并让所有按钮恢复朴素。
// 与抓取互斥：停止后关闭抓取（停止朗读后不应继续弹出悬浮框）
$("p-stop").addEventListener("click", () => {
  TTS.stop();
  if (window.__TAURI__?.event) {
    window.__TAURI__.event.emit("stop-audio", {});
  }
  // 停止时无条件关闭抓取（与 grabActive 是否命中无关），确保与后台状态彻底互斥
  if (window.__TAURI__?.core) {
    grabActive = false;
    window.__TAURI__.core.invoke("toggle_grab", { on: false }).catch(() => {});
  }
  setActive("p-read", false);
  setActive("p-export", false);
  setActive("p-grab", false);
  showHint("已停止（抓取已关闭）");
});

// 导出 MP3：合成并写入 Downloads，完成时经 export-done 提示
$("p-export").addEventListener("click", () => {
  setActive("p-export", true);
  showHint("正在合成导出...");
  TTS.export(currentText()).catch((e) => {
    setActive("p-export", false);
    showHint("导出失败：" + e.message, true);
  });
});

// 抓取开关：默认开启（启动即 arm，全局选中即弹悬浮框）；点击切换关闭/开启
let grabActive = true;
$("p-grab").addEventListener("click", async () => {
  try {
    if (window.__TAURI__?.core) {
      grabActive = !grabActive;
      await window.__TAURI__.core.invoke("toggle_grab", { on: grabActive });
      setActive("p-grab", grabActive);
      showHint(grabActive ? "抓取已开启，请用鼠标选中要朗读的文字..." : "抓取已关闭");
    } else {
      // 浏览器预览兜底：直接读剪贴板
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

// 音色选择
$("sel-voice").addEventListener("change", (e) => TTS.voice(e.target.value));

$("sel-rate").addEventListener("change", (e) => TTS.rate(parseInt(e.target.value, 10)));
$("sel-pitch").addEventListener("change", (e) => TTS.pitch(e.target.value));

// 退出：调用 Rust 关闭 sidecar 子进程并退出应用
$("p-quit").addEventListener("click", () => {
  showHint("正在退出...");
  TTS.quit().catch((e) => showHint("退出失败：" + e.message, true));
});

// ---- 跳过注入应用管理 ----
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

// 跳过应用被添加时刷新列表（Rust 侧广播）
window.__TAURI__.event?.listen("skip-app-added", () => {
  refreshSkipList();
});

// 选取窗口：点击后把鼠标移到目标窗口，2 秒后自动识别并加入跳过
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

// 释放所选：删除下拉框中选中的跳过应用
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

// ---- 跳过抓取的应用（不读取该窗口文字）----

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

// 面板加载时刷新一次跳过列表（内部自带重试，不依赖 __TAURI__ 注入时序）
setTimeout(refreshSkipList, 800);

// ---- 模型穿透 + 快捷键设置 ----

function ctLabel(on) { return on ? "穿透中" : "可交互"; }
function setCtState(on) {
  setActive("p-ct", !!on);
  const b = $("p-ct");
  if (b) b.textContent = ctLabel(on);
}

// 穿透开关：点击切换（写回设置 + 广播）
$("p-ct").addEventListener("click", async () => {
  try {
    const next = await window.__TAURI__.core.invoke("toggle_click_through");
    setCtState(next);
    showHint(next ? "已开启穿透（不挡鼠标）" : "已关闭穿透（可交互）");
  } catch (err) {
    showHint("切换失败：" + err.message, true);
  }
});

// 其他入口切换穿透时同步按钮状态（Rust 广播）
window.__TAURI__.event?.listen("click-through-changed", (e) => {
  setCtState(!!e.payload);
});

// 快捷键捕获：点击按钮 → 按新组合键 → 保存到设置
let hkCapture = null; // { el, key }
function hkFriendly(s) {
  return s.replace(/Key([A-Z])/g, "$1").replace(/Digit(\d)/g, "$1");
}
function bindHkCapture(btnId, key) {
  $(btnId).addEventListener("click", () => {
    hkCapture = { el: $(btnId), key };
    $(btnId).textContent = "按新组合键...";
  });
}
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

async function saveHk(key, str) {
  try {
    const s = await window.__TAURI__.core.invoke("get_app_settings");
    s[key] = str;
    await window.__TAURI__.core.invoke("set_app_settings", { settings: s });
  } catch (err) {
    showHint("保存快捷键失败：" + err.message, true);
  }
}

bindHkCapture("p-hk-panel", "hotkey_panel");
bindHkCapture("p-hk-ct", "hotkey_ct");

// ---- 悬浮框背景：颜色 + 透明度 ----
function setFloaterStyleUI(s) {
  const flc = $("p-fl-color"), flo = $("p-fl-op"), fol = $("p-fl-op-lb");
  if (!flc || !flo) return;
  flc.value = /^#[0-9a-fA-F]{6}$/.test(s.floater_color) ? s.floater_color : "#1e2026";
  const op = (typeof s.floater_opacity === "number" && s.floater_opacity >= 0 && s.floater_opacity <= 1)
    ? s.floater_opacity : 0.84;
  flo.value = Math.round(op * 100);
  if (fol) fol.textContent = flo.value + "%";
}

let flStyleTimer = null;
function persistFloaterStyle() {
  const flc = $("p-fl-color"), flo = $("p-fl-op"), fol = $("p-fl-op-lb");
  if (!flc || !flo) return;
  if (fol) fol.textContent = flo.value + "%";
  clearTimeout(flStyleTimer);
  flStyleTimer = setTimeout(async () => {
    if (!window.__TAURI__?.core) return;
    try {
      const s = await window.__TAURI__.core.invoke("get_app_settings");
      s.floater_color = flc.value;
      s.floater_opacity = (Math.min(100, Math.max(0, parseInt(flo.value, 10) || 0))) / 100;
      await window.__TAURI__.core.invoke("set_app_settings", { settings: s });
    } catch (_) {}
  }, 300);
}
(function bindFloaterStyleUI() {
  const flc = $("p-fl-color"), flo = $("p-fl-op");
  if (flc) flc.addEventListener("input", persistFloaterStyle);
  if (flo) flo.addEventListener("input", persistFloaterStyle);
})();

// ---- 精灵问候语 ----
function setGreetingUI(s) {
  const inp = $("p-greeting");
  if (!inp) return;
  inp.value = (s.greeting && s.greeting.trim()) ? s.greeting : "";
  inp.placeholder = "精灵首次弹出的问候语";
}

let greetingTimer = null;
function persistGreeting() {
  const inp = $("p-greeting");
  if (!inp || !window.__TAURI__?.core) return;
  clearTimeout(greetingTimer);
  greetingTimer = setTimeout(async () => {
    try {
      const s = await window.__TAURI__.core.invoke("get_app_settings");
      s.greeting = (inp.value || "").trim() || "你好，我是桌面小精灵，欢迎回来！";
      await window.__TAURI__.core.invoke("set_app_settings", { settings: s });
      showHint("问候语已保存");
    } catch (_) {}
  }, 400);
}
(function bindGreetingUI() {
  const inp = $("p-greeting");
  if (inp) inp.addEventListener("input", persistGreeting);
})();

// 启动时读取设置：同步穿透开关与快捷键标签
(async function initSettings() {
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
})();

// ---- 忽略括号内容：开关 + 自定义符号对 ----
const DEFAULT_IGNORE_SYMS = ["[]", "{}", "【】", "（）", "()", "《》", "<>"];

function setIgnoreUI(s) {
  const cb = $("p-ignore"), inp = $("p-ignore-syms");
  if (!cb || !inp) return;
  cb.checked = s.ignore_pairs !== false;
  const syms = Array.isArray(s.ignore_symbols) && s.ignore_symbols.length ? s.ignore_symbols : DEFAULT_IGNORE_SYMS;
  inp.value = syms.join(",");
}

let ignoreTimer = null;
function persistIgnore() {
  const cb = $("p-ignore"), inp = $("p-ignore-syms");
  if (!cb || !inp || !window.__TAURI__?.core) return;
  clearTimeout(ignoreTimer);
  ignoreTimer = setTimeout(async () => {
    try {
      const s = await window.__TAURI__.core.invoke("get_app_settings");
      s.ignore_pairs = cb.checked;
      // 解析用户输入的符号对：按逗号分隔，取每项首尾字符
      const raw = (inp.value || "").split(/[,，]/).map((x) => x.trim()).filter(Boolean);
      const syms = [];
      raw.forEach((x) => {
        if (x.length >= 2) syms.push(x[0] + x[x.length - 1]);
      });
      s.ignore_symbols = syms.length ? syms : DEFAULT_IGNORE_SYMS;
      await window.__TAURI__.core.invoke("set_app_settings", { settings: s });
    } catch (_) {}
  }, 300);
}
(function bindIgnoreUI() {
  const cb = $("p-ignore"), inp = $("p-ignore-syms");
  if (cb) cb.addEventListener("change", persistIgnore);
  if (inp) inp.addEventListener("input", persistIgnore);
})();

// ---- 右键菜单：隐藏面板 ----
const ctxMenu = $("panel-ctx");
document.addEventListener("contextmenu", (e) => {
  // 文本框/输入框内保留系统右键菜单（粘贴等），其余区域弹出隐藏菜单
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

// ---- 管理自导入音色 API ----
let __apiEditing = null; // 当前编辑的 provider 名，null=新建

function typeLabel(t) {
  if (t === "azure") return "Azure 语音";
  if (t === "custom") return "通用 HTTP";
  return "OpenAI 兼容";
}
function _mkBtn(text, fn, del) {
  const b = document.createElement("button");
  b.className = "mitem" + (del ? " del" : "");
  b.textContent = text;
  b.addEventListener("click", fn);
  return b;
}

async function openApiMgr() {
  const mgr = $("api-mgr");
  if (!mgr || !window.__TAURI__?.core) return;
  mgr.classList.remove("modal-hidden");
  resetApiForm();
  await refreshApiList();
}

function closeApiMgr() { const mgr = $("api-mgr"); if (mgr) mgr.classList.add("modal-hidden"); }

async function refreshApiList() {
  const data = await TTS.providers();
  window.__providerList = (data && data.providers) || [];
  const listEl = $("api-mgr-list");
  if (!listEl) return;
  listEl.innerHTML = "";
  if (!window.__providerList.length) {
    const d = document.createElement("div");
    d.className = "api-item";
    d.textContent = "（还没有自导入的 API，请在下方添加）";
    listEl.appendChild(d);
    return;
  }
  window.__providerList.forEach((p) => {
    const item = document.createElement("div");
    item.className = "api-item";
    const nm = document.createElement("div");
    nm.className = "nm";
    nm.textContent = p.name + " · " + typeLabel(p.type) + " · " + providerVoices(p).length + " 音色";
    nm.title = JSON.stringify(p);
    item.appendChild(nm);
    item.appendChild(_mkBtn("拉取音色", () => doFetch(p.name), false));
    item.appendChild(_mkBtn("编辑", () => { __apiEditing = p.name; fillApiForm(p); }, false));
    item.appendChild(_mkBtn("删除", () => removeProvider(p.name), true));
    listEl.appendChild(item);
  });
}

async function doFetch(name) {
  showHint("正在拉取 " + name + " 的音色...");
  try {
    const voices = await TTS.fetchProviderVoices(name);
    const p = window.__providerList.find((x) => x.name === name);
    if (p) p.voices = voices;
    await TTS.saveProviders({ providers: window.__providerList });
    buildVoiceOptions($("sel-voice").value);
    await refreshApiList();
    showHint("已拉取 " + voices.length + " 个音色（" + name + "）");
  } catch (e) {
    showHint("拉取失败：" + e.message, true);
  }
}

function syncTypeFields() {
  const t = $("f-type").value;
  const az = $("f-azure"), oa = $("f-openai");
  if (az) az.classList.toggle("fhide", t !== "azure");
  if (oa) oa.classList.toggle("fhide", t === "azure");
}

function resetApiForm() {
  __apiEditing = null;
  ["f-name", "f-region", "f-key", "f-base", "f-key-openai"].forEach((id) => { const el = $(id); if (el) el.value = ""; });
  const m = $("f-model"); if (m) m.value = "tts-1";
  const t = $("f-type"); if (t) t.value = "azure";
  syncTypeFields();
}

function fillApiForm(p) {
  __apiEditing = p.name;
  ["f-region", "f-base", "f-key", "f-key-openai"].forEach((id) => { const el = $(id); if (el) el.value = ""; });
  const n = $("f-name"); if (n) n.value = p.name;
  const t = $("f-type"); if (t) t.value = p.type || "openai";
  ["region", "base", "key"].forEach((k) => { const el = $("f-" + k); if (el && p[k] != null) el.value = p[k]; });
  const m = $("f-model"); if (m) m.value = p.model || "tts-1";
  const ko = $("f-key-openai"); if (ko && p.key) ko.value = p.key;
  syncTypeFields();
}

async function saveApiForm() {
  const name = ($("f-name").value || "").trim();
  if (!name) { showHint("请填写 Provider 名称", true); return; }
  if (/[:"]/.test(name)) { showHint("名称不能包含冒号或引号", true); return; }
  const t = $("f-type").value;
  const p = { name };
  if (t === "azure") {
    p.type = "azure";
    p.region = ($("f-region").value || "").trim();
    p.key = ($("f-key").value || "").trim();
  } else {
    p.type = t;
    p.base = ($("f-base").value || "").trim();
    p.model = ($("f-model").value || "").trim() || "tts-1";
    p.key = ($("f-key-openai").value || "").trim();
  }
  // 编辑时保留已拉取的音色
  const exist = window.__providerList.find((x) => x.name === name);
  if (exist && exist.voices) p.voices = exist.voices;
  const idx = window.__providerList.findIndex((x) => x.name === name);
  if (idx >= 0) window.__providerList[idx] = p;
  else window.__providerList.push(p);
  try {
    await TTS.saveProviders({ providers: window.__providerList });
    buildVoiceOptions($("sel-voice").value || ("api:" + _safeName(name) + ":" + (providerVoices(p)[0] || "")));
    await refreshApiList();
    resetApiForm();
    showHint("已保存：" + name);
  } catch (e) {
    showHint("保存失败：" + e.message, true);
  }
}

function removeProvider(name) {
  __apiEditing = null;
  window.__providerList = window.__providerList.filter((x) => x.name !== name);
  TTS.saveProviders({ providers: window.__providerList })
    .then(() => {
      buildVoiceOptions($("sel-voice").value);
      refreshApiList();
      showHint("已删除：" + name);
    })
    .catch((e) => showHint("删除失败：" + e.message, true));
}

// 绑定事件
(function bindApiMgr() {
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
})();