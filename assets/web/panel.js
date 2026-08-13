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
  quit() {
    if (!window.__TAURI__?.core) return Promise.resolve();
    return window.__TAURI__.core.invoke("quit");
  },
};

const $ = (id) => document.getElementById(id);
let mode = "watch";

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

// 设置暂停/开始按钮文字
function setPauseLabel(label) {
  const btn = $("p-pause");
  if (btn) btn.textContent = label;
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
    setActive("p-pause", s === "paused");
    setPauseLabel(s === "paused" ? "开始" : "暂停");
    if (s === "idle") {
      setActive("p-export", false);
      setPauseLabel("暂停");
      // 注意：不重置 p-grab —— 抓取是独立开关（启动即 arm），与播放状态无关
    }
  });

  // 朗读进度：主窗口广播 frac(0-1) + 当前块内秒数，驱动进度条
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

  // 进入面板时广播就绪（Rust 侧可据此补发状态）
  window.__TAURI__.event.emit("panel-ready", {});

  // Esc 关闭面板
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") window.__TAURI__.event.emit("panel-closing", {});
  });

  // 启动时读取持久化配置并同步下拉框（音色/语速/语调）
  TTS.getSettings().then((s) => {
    if (!s) return;
    if (s.voice && $("sel-voice")) $("sel-voice").value = s.voice;
    if (s.rate != null && $("sel-rate")) $("sel-rate").value = String(s.rate);
    if (s.pitch && $("sel-pitch")) $("sel-pitch").value = s.pitch;
  });
});

// 朗读：读文本区内容（空则用默认句），播放中按钮变蓝
$("p-read").addEventListener("click", () => {
  setActive("p-read", true);
  setActive("p-pause", false);
  setPauseLabel("暂停");
  TTS.read(currentText()).catch((e) => {
    setActive("p-read", false);
    showHint("朗读失败：" + e.message, true);
  });
});

// 暂停/开始：切换主窗口音频的暂停状态（按钮文字随状态变化）
$("p-pause").addEventListener("click", () => {
  if (!$("p-pause").classList.contains("active")) {
    // 当前是"暂停"，点击后暂停
    if (window.__TAURI__?.event) window.__TAURI__.event.emit("pause-audio", {});
  } else {
    // 当前是"开始"，点击后恢复
    if (window.__TAURI__?.event) window.__TAURI__.event.emit("resume-audio", {});
  }
});

// 停止：广播给主窗口真实停止音频，同时通知 sidecar；并让所有按钮恢复朴素
$("p-stop").addEventListener("click", () => {
  TTS.stop();
  if (window.__TAURI__?.event) {
    window.__TAURI__.event.emit("stop-audio", {});
  }
  setActive("p-read", false);
  setActive("p-export", false);
  setActive("p-pause", false);
  setPauseLabel("暂停");
  // 注意：不重置 p-grab —— 抓取是独立开关（启动即 arm），停止朗读不关闭抓取
  showHint("已停止");
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

$("sel-voice").addEventListener("change", (e) => TTS.voice(e.target.value));
$("sel-rate").addEventListener("change", (e) => TTS.rate(parseInt(e.target.value, 10)));
$("sel-pitch").addEventListener("change", (e) => TTS.pitch(e.target.value));

// 退出：调用 Rust 关闭 sidecar 子进程并退出应用
$("p-quit").addEventListener("click", () => {
  showHint("正在退出...");
  TTS.quit().catch((e) => showHint("退出失败：" + e.message, true));
});