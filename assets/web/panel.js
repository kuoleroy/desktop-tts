// 面板窗口：交互模式载体（不穿透，可点击）
const TTS = {
  read(text) {
    return window.__TAURI__.core.invoke("read_text", { text });
  },
  stop() {
    return window.__TAURI__.core.invoke("stop_read");
  },
  voice(name) {
    return window.__TAURI__.core.invoke("set_voice", { name });
  },
  rate(r) {
    return window.__TAURI__.core.invoke("set_rate", { rate: r });
  },
  pitch(p) {
    return window.__TAURI__.core.invoke("set_pitch", { pitch: p });
  },
};

const $ = (id) => document.getElementById(id);
let mode = "watch";

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
  window.__TAURI__.event.listen("toggle-mode", (e) => {
    mode = e.payload;
    document.title = "Pet Panel [" + mode + "]";
    window.__TAURI__.event.emit("mode-confirmed", "panel:" + mode);
    document.body.dataset.mode = mode;
  });
  $("p-back").addEventListener("click", () => {
    window.__TAURI__.event.emit("panel-closing", {});
  });
  $("p-quit").addEventListener("click", () => {
    window.__TAURI__.core.invoke("quit");
  });
  // 进入面板时广播就绪（Rust 侧可据此补发状态）
  window.__TAURI__.event.emit("panel-ready", {});
});

$("p-read").addEventListener("click", () => {
  TTS.read("你好，我是桌面小精灵。选中文字，我就能帮你朗读出来。");
});
$("p-stop").addEventListener("click", () => TTS.stop());
$("sel-voice").addEventListener("change", (e) => TTS.voice(e.target.value));
$("sel-rate").addEventListener("change", (e) => TTS.rate(parseInt(e.target.value, 10)));
$("sel-pitch").addEventListener("change", (e) => TTS.pitch(e.target.value));