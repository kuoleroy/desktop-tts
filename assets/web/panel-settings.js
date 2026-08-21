// 面板设置模块：快捷键、悬浮框样式、问候语、忽略符号、缓存管理等

// ---- 模型穿透 + 快捷键设置 ----
function ctLabel(on) { return on ? "穿透中" : "可交互"; }
function setCtState(on) {
  setActive("p-ct", !!on);
  const b = $("p-ct");
  if (b) b.textContent = ctLabel(on);
}

// 快捷键友好显示
function hkFriendly(s) {
  return s.replace(/Key([A-Z])/g, "$1").replace(/Digit(\d)/g, "$1");
}

let hkCapture = null;
function bindHkCapture(btnId, key) {
  $(btnId).addEventListener("click", () => {
    hkCapture = { el: $(btnId), key };
    $(btnId).textContent = "按新组合键...";
  });
}

async function saveHk(key, str) {
  try {
    const s = await window.__TAURI__.core.invoke("get_app_settings");
    s[key] = str;
    await window.__TAURI__.core.invoke("set_app_settings", { settings: s });
  } catch (err) {
    showHint("保存快捷键失败：" + err.message, true);
  }
}

function bindHkCaptureGlobal() {
  bindHkCapture("p-hk-panel", "hotkey_panel");
  bindHkCapture("p-hk-ct", "hotkey_ct");
}

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

function bindFloaterStyleUI() {
  const flc = $("p-fl-color"), flo = $("p-fl-op");
  if (flc) flc.addEventListener("input", persistFloaterStyle);
  if (flo) flo.addEventListener("input", persistFloaterStyle);
}

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

function bindGreetingUI() {
  const inp = $("p-greeting");
  if (inp) inp.addEventListener("input", persistGreeting);
}

// ---- 多开 ----
function setMultiUI(s) {
  const m = $("p-multi");
  if (m) m.checked = !!s.multi_instance;
}

function bindMultiUI() {
  const m = $("p-multi");
  if (!m) return;
  m.addEventListener("change", async () => {
    try {
      const s = await window.__TAURI__.core.invoke("get_app_settings");
      s.multi_instance = m.checked;
      await window.__TAURI__.core.invoke("set_app_settings", { settings: s });
      showHint("多开设置已保存");
    } catch (_) {}
  });
}

// ---- 忽略括号内容 ----
const DEFAULT_IGNORE_SYMS = ["[]", "{}", "【】", "（）", "()", "《》", "<>"];
const DEFAULT_STRIP_CHARS = "*~`#>|_-";

function setIgnoreUI(s) {
  const cb = $("p-ignore"), inp = $("p-ignore-syms");
  if (!cb || !inp) return;
  cb.checked = s.ignore_pairs !== false;
  const syms = Array.isArray(s.ignore_symbols) && s.ignore_symbols.length ? s.ignore_symbols : DEFAULT_IGNORE_SYMS;
  inp.value = syms.join(",");
  const stripCb = $("p-strip-syms"), stripInp = $("p-strip-chars");
  if (stripCb) stripCb.checked = s.strip_symbols !== false;
  if (stripInp) {
    const custom = typeof s.strip_symbol_chars === "string" && s.strip_symbol_chars ? s.strip_symbol_chars : DEFAULT_STRIP_CHARS;
    stripInp.value = custom;
  }
}

let ignoreTimer = null;
function persistIgnore() {
  const cb = $("p-ignore"), inp = $("p-ignore-syms");
  const stripCb = $("p-strip-syms"), stripInp = $("p-strip-chars");
  if (!cb || !inp || !window.__TAURI__?.core) return;
  clearTimeout(ignoreTimer);
  ignoreTimer = setTimeout(async () => {
    try {
      const s = await window.__TAURI__.core.invoke("get_app_settings");
      s.ignore_pairs = cb.checked;
      const raw = (inp.value || "").split(/[,，]/).map((x) => x.trim()).filter(Boolean);
      const syms = [];
      raw.forEach((x) => {
        if (x.length >= 2) syms.push(x[0] + x[x.length - 1]);
      });
      s.ignore_symbols = syms.length ? syms : DEFAULT_IGNORE_SYMS;
      if (stripCb) s.strip_symbols = stripCb.checked;
      if (stripInp) {
        s.strip_symbol_chars = (stripInp.value || "").trim() || DEFAULT_STRIP_CHARS;
      }
      await window.__TAURI__.core.invoke("set_app_settings", { settings: s });
    } catch (_) {}
  }, 300);
}

function bindIgnoreUI() {
  const cb = $("p-ignore"), inp = $("p-ignore-syms");
  if (cb) cb.addEventListener("change", persistIgnore);
  if (inp) inp.addEventListener("input", persistIgnore);
  const stripCb = $("p-strip-syms"), stripInp = $("p-strip-chars");
  if (stripCb) stripCb.addEventListener("change", persistIgnore);
  if (stripInp) stripInp.addEventListener("input", persistIgnore);
}

// ---- 朗读缓存管理 ----
async function refreshCacheInfo() {
  if (!window.__TAURI__?.core) return;
  try {
    const info = await window.__TAURI__.core.invoke("get_cache_info");
    const sizeEl = $("p-cache-size");
    if (sizeEl) {
      const mb = Number(info.size_mb || 0);
      sizeEl.textContent = "当前占用 " + (mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : mb.toFixed(0) + " MB")
        + " / " + (info.files || 0) + " 个文件" + (info.limit_mb ? "（上限 " + info.limit_mb + " MB）" : "（不限）");
    }
    const lim = $("p-cache-limit");
    if (lim) lim.value = info.limit_mb;
  } catch (_) {}
}

let cacheLimitTimer = null;
function persistCacheLimit() {
  const lim = $("p-cache-limit");
  if (!lim || !window.__TAURI__?.core) return;
  clearTimeout(cacheLimitTimer);
  cacheLimitTimer = setTimeout(async () => {
    try {
      await window.__TAURI__.core.invoke("set_cache_limit", { mb: parseInt(lim.value, 10) || 0 });
      showHint("缓存上限已保存");
      refreshCacheInfo();
    } catch (_) {}
  }, 400);
}

function bindCacheUI() {
  const lim = $("p-cache-limit"), open = $("p-cache-open"), clear = $("p-cache-clear");
  if (lim) lim.addEventListener("change", persistCacheLimit);
  if (open) open.addEventListener("click", async () => {
    try {
      const info = await window.__TAURI__.core.invoke("get_cache_info");
      if (info && info.dir) await window.__TAURI__.core.invoke("open_folder", { path: info.dir });
    } catch (_) {}
  });
  if (clear) clear.addEventListener("click", async () => {
    try {
      const n = await window.__TAURI__.core.invoke("clear_cache");
      showHint("已清空缓存（" + n + " 个文件）");
      refreshCacheInfo();
    } catch (_) {}
  });
  refreshCacheInfo();
}