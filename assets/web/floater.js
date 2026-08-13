let currentText = "";
const $ = (id) => document.getElementById(id);

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