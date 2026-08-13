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
  window.__TAURI__.event.listen("toggle-mode", (e) => {
    mode = e.payload;
    document.title = "Pet Panel [" + mode + "]";
    window.__TAURI__.event.emit("mode-confirmed", "panel:" + mode);
    document.body.dataset.mode = mode;
  });
  // 导出完成：Rust 广播绝对路径
  window.__TAURI__.event.listen("export-done", (e) => {
    showHint("已导出：" + e.payload);
  });

  // 进入面板时广播就绪（Rust 侧可据此补发状态）
  window.__TAURI__.event.emit("panel-ready", {});
});

// 朗读：读文本区内容（空则用默认句）
$("p-read").addEventListener("click", () => {
  TTS.read(currentText()).catch((e) => showHint("朗读失败：" + e.message, true));
});

// 停止：广播给主窗口真实停止音频，同时通知 sidecar
$("p-stop").addEventListener("click", () => {
  TTS.stop();
  if (window.__TAURI__?.event) {
    window.__TAURI__.event.emit("stop-audio", {});
  }
  showHint("已停止");
});

// 导出 MP3：合成并写入 Downloads，完成时经 export-done 提示
$("p-export").addEventListener("click", () => {
  showHint("正在合成导出...");
  TTS.export(currentText()).catch((e) => showHint("导出失败：" + e.message, true));
});

// 抓取朗读：读取系统剪贴板文字并朗读
$("p-grab").addEventListener("click", async () => {
  try {
    const txt = await navigator.clipboard.readText();
    if (!txt || !txt.trim()) {
      showHint("剪贴板为空", true);
      return;
    }
    TTS.read(txt).catch((e) => showHint("朗读失败：" + e.message, true));
    showHint("已抓取剪贴板朗读");
  } catch (e) {
    showHint("读取剪贴板失败，请先在文本中 Ctrl+C", true);
  }
});

$("sel-voice").addEventListener("change", (e) => TTS.voice(e.target.value));
$("sel-rate").addEventListener("change", (e) => TTS.rate(parseInt(e.target.value, 10)));
$("sel-pitch").addEventListener("change", (e) => TTS.pitch(e.target.value));