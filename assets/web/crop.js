const $ = (id) => document.getElementById(id);
const box = $("box");
let start = null;
let active = false;

function hideWin() {
  try { window.__TAURI__.window.getCurrentWindow().hide(); } catch (_) {}
}
function clearUi() {
  active = false;
  start = null;
  box.style.display = "none";
  box.style.left = "0px";
  box.style.top = "0px";
  box.style.width = "0px";
  box.style.height = "0px";
}

document.addEventListener("mousedown", (e) => {
  if (e.button !== 0) return;
  active = true;
  start = { x: e.clientX, y: e.clientY };
  box.style.display = "block";
  box.style.left = start.x + "px";
  box.style.top = start.y + "px";
  box.style.width = "0px";
  box.style.height = "0px";
});

document.addEventListener("mousemove", (e) => {
  if (!active || !start) return;
  const x = Math.min(start.x, e.clientX);
  const y = Math.min(start.y, e.clientY);
  const w = Math.abs(e.clientX - start.x);
  const h = Math.abs(e.clientY - start.y);
  box.style.left = x + "px";
  box.style.top = y + "px";
  box.style.width = w + "px";
  box.style.height = h + "px";
});

document.addEventListener("mouseup", (e) => {
  if (!active || !start) return;
  const x1 = Math.round(start.x);
  const y1 = Math.round(start.y);
  const x2 = Math.round(e.clientX);
  const y2 = Math.round(e.clientY);
  const l = Math.min(x1, x2);
  const t = Math.min(y1, y2);
  const r = Math.max(x1, x2);
  const b = Math.max(y1, y2);
  active = false;
  start = null;
  // 过小的框视为误触，忽略
  if (r - l < 10 || b - t < 10) {
    clearUi();
    hideWin();
    return;
  }
  // 屏幕坐标（全屏窗口左上角即屏幕左上角）
  const rect = [l, t, r, b];
  clearUi();
  hideWin();
  try {
    window.__TAURI__.core.invoke("ocr_rect", { rect: JSON.stringify(rect) }).catch(() => {});
  } catch (_) {}
});

// Esc 取消
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { clearUi(); hideWin(); }
});
$("cancel").addEventListener("click", () => { clearUi(); hideWin(); });

// 防止右键菜单
document.addEventListener("contextmenu", (e) => e.preventDefault());