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

// ---- 朗读历史 / 收藏夹（localStorage 持久化，重启不丢）----
const HIST_KEY = "pet_tts_history";
const MAX_HIST = 50;

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HIST_KEY) || "[]"); }
  catch (e) { return []; }
}
function saveHistory(list) {
  try { localStorage.setItem(HIST_KEY, JSON.stringify(list)); } catch (e) {}
}

// 记录一条历史：重复文本置顶，收藏保留；超限裁剪
function recordHistory(text) {
  text = (text || "").trim();
  if (!text) return;
  const list = loadHistory();
  const existing = list.find((it) => it.text === text);
  if (existing) {
    existing.time = Date.now();
    const idx = list.indexOf(existing);
    list.splice(idx, 1);
    list.unshift(existing);
  } else {
    list.unshift({ text, time: Date.now(), fav: false });
  }
  if (list.length > MAX_HIST) list.length = MAX_HIST;
  saveHistory(list);
  renderHistory();
}

// 渲染历史列表：收藏固定最前，其余按时间倒序
function renderHistory() {
  const list = loadHistory();
  const wrap = $("hist-list");
  if (!wrap) return;
  if (!list.length) {
    wrap.innerHTML = '<div class="hist-empty">暂无历史，朗读/抓取的文字会记录在这里</div>';
    return;
  }
  const favs = list.filter((it) => it.fav);
  const rest = list.filter((it) => !it.fav);
  const items = favs.concat(rest);
  wrap.innerHTML = "";
  items.forEach((it) => {
    const row = document.createElement("div");
    row.className = "hist-item";
    const star = document.createElement("span");
    star.className = "hist-star" + (it.fav ? " on" : "");
    star.textContent = it.fav ? "★" : "☆";
    star.title = it.fav ? "取消收藏" : "收藏";
    star.addEventListener("click", () => {
      const l = loadHistory();
      const t = l.find((x) => x.text === it.text);
      if (t) t.fav = !t.fav;
      saveHistory(l);
      renderHistory();
    });
    const txt = document.createElement("span");
    txt.className = "hist-text";
    txt.textContent = it.text;
    txt.title = it.text;
    txt.addEventListener("click", () => { $("ptext").value = it.text; });
    const del = document.createElement("span");
    del.className = "hist-del";
    del.textContent = "×";
    del.title = "删除";
    del.addEventListener("click", () => {
      const l = loadHistory().filter((x) => x.text !== it.text);
      saveHistory(l);
      renderHistory();
    });
    row.appendChild(star);
    row.appendChild(txt);
    row.appendChild(del);
    wrap.appendChild(row);
  });
}

// 清空历史（保留收藏？此处保留收藏，只清非收藏）——或全清。这里提供两个：点文本区可只删单条，清空按钮删全部
$("hist-clear").addEventListener("click", () => {
  if (!confirm("确认清空全部历史与收藏？")) return;
  saveHistory([]);
  renderHistory();
});

// 朗读后记录历史（导出、抓取也可在此追加；当前朗读即入历史）
$("p-read").addEventListener("click", () => {
  recordHistory(currentText());
});

renderHistory();