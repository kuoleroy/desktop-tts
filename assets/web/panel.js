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

// 返回观赏 / 退出：绑定到顶层，真实 Tauri 走事件/命令；纯浏览器预览走兜底
$("p-back").addEventListener("click", () => {
  if (window.__TAURI__?.event) {
    // 真实应用：通知 Rust 关闭面板并切回观赏模式
    window.__TAURI__.event.emit("panel-closing", {});
  } else {
    // 浏览器预览：无 Tauri 运行时，导航回主视图模拟"返回观赏"
    window.location.href = "index.html";
  }
});
$("p-quit").addEventListener("click", () => {
  if (window.__TAURI__?.core) window.__TAURI__.core.invoke("quit");
});

whenTauriReady(() => {
  // 面板拖拽移动：按住标题栏可拖动窗口（无边框窗口无系统标题栏）
  const head = document.querySelector(".panel-head");
  if (head) {
    head.addEventListener("mousedown", (e) => {
      // 避免在按钮上按下触发拖拽（返回观赏按钮）
      if (e.target.closest("button")) return;
      try {
        window.__TAURI__.window.getCurrentWindow().startDragging();
      } catch (err) {
        showHint("拖动失败：" + (err && err.message || err), true);
      }
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
      setActive("p-grab", false);
      setPauseLabel("暂停");
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
  setActive("p-grab", false);
  setActive("p-pause", false);
  setPauseLabel("暂停");
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

// 抓取开关：开启后持续监控鼠标选区，选中文字即移动面板并填充文本；再次点击关闭
let grabActive = false;
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