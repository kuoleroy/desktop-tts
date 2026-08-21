// 右键菜单 + Tauri 事件绑定模块
import { loadVRM, modelUrl, getCurrentModel, getVrm } from "./app-model.js";
import { loadDance, getDanceName, DEFAULT_DANCE } from "./app-dance.js";
import { playAudioFrom, startQueue, stopAudio, pauseAudio, resumeAudio } from "./app-audio.js";

const $ = (id) => document.getElementById(id);

export function initMenu(viewRot, viewZoom, setStatusFn) {
  const menu = document.getElementById("pet-menu");
  if (!menu) return;

  let rightDrag = { on: false, startX: 0, startY: 0, lastX: 0, lastY: 0, moved: false };

  document.addEventListener("contextmenu", (e) => e.preventDefault());
  document.addEventListener("mousedown", (e) => {
    if (e.button === 2) {
      rightDrag.on = true; rightDrag.moved = false;
      rightDrag.startX = rightDrag.lastX = e.clientX;
      rightDrag.startY = rightDrag.lastY = e.clientY;
    }
  });
  document.addEventListener("mousemove", (e) => {
    if (!rightDrag.on) return;
    const dx = e.clientX - rightDrag.lastX;
    const dy = e.clientY - rightDrag.lastY;
    if (Math.hypot(e.clientX - rightDrag.startX, e.clientY - rightDrag.startY) > 20) rightDrag.moved = true;
    rightDrag.lastX = e.clientX;
    rightDrag.lastY = e.clientY;
    viewRot.y -= dx * 0.008;
    viewRot.x = Math.max(-1.4, Math.min(1.4, viewRot.x - dy * 0.005));
  });
  document.addEventListener("mouseup", (e) => {
    if (e.button === 2 && rightDrag.on && !rightDrag.moved && menu) {
      menu.classList.remove("hidden");
      menu.style.left = Math.min(e.clientX, window.innerWidth - menu.offsetWidth - 4) + "px";
      menu.style.top = Math.min(e.clientY, window.innerHeight - menu.offsetHeight - 4) + "px";
      const showItem = menu.querySelector('[data-action="show-panel"]');
      if (showItem) {
        window.__TAURI__.core.invoke("get_panel_visible").then((vis) => {
          showItem.textContent = vis ? "隐藏面板" : "显示面板";
        }).catch(() => {});
      }
      _populateModelSub();
      _populateDanceSub();
    }
    rightDrag.on = false;
  });
  document.addEventListener("mouseleave", () => { rightDrag.on = false; });
  document.addEventListener("click", (e) => {
    if (menu && !menu.contains(e.target)) menu.classList.add("hidden");
  });

  // 菜单项点击
  menu.querySelectorAll(".menu-item").forEach((item) => {
    item.addEventListener("click", async () => {
      const a = item.dataset.action;
      if (a === "show-panel") {
        try { await window.__TAURI__.core.invoke("toggle_panel_ui"); } catch (_) {}
      } else if (a === "reset-size") {
        try {
          const win = window.__TAURI__.window.getCurrentWindow();
          const W = window.__TAURI__.window;
          const size = new W.PhysicalSize(240, 300);
          await win.setSize(size);
          setStatusFn("已恢复默认大小");
        } catch (err) {
          setStatusFn("恢复失败: " + (err?.message || err));
        }
      }
      menu.classList.add("hidden");
    });
  });

  // 子菜单显隐
  menu.querySelectorAll(".menu-item.has-sub").forEach((item) => {
    const sub = item.querySelector(".submenu");
    if (!sub) return;
    sub.classList.add("sub-hidden");
    let hideTimer = null;
    const hide = () => {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => sub.classList.add("sub-hidden"), 100);
    };
    const show = () => {
      clearTimeout(hideTimer);
      sub.classList.remove("sub-hidden");
      try {
        const sw = sub.offsetWidth || 160, sh = sub.offsetHeight || 200;
        const r = item.getBoundingClientRect();
        let x = r.right + 2;
        if (x + sw > window.innerWidth) x = r.left - sw - 2;
        let y = r.top;
        if (y + sh > window.innerHeight) y = Math.max(0, window.innerHeight - sh - 4);
        sub.style.left = Math.max(0, x) + "px";
        sub.style.top = Math.max(0, y) + "px";
      } catch (_) {}
    };
    item.addEventListener("mouseleave", hide);
    item.addEventListener("mouseenter", show);
    sub.addEventListener("mouseenter", show);
    sub.addEventListener("mouseleave", hide);
  });

  // 窗口大小子菜单
  menu.querySelectorAll(".sub-item").forEach((item) => {
    item.addEventListener("click", async () => {
      const scale = parseFloat(item.dataset.scale);
      if (isNaN(scale)) return;
      try {
        await window.__TAURI__.core.invoke("set_main_scale", { scale });
        setStatusFn("窗口大小 " + item.textContent.trim());
      } catch (err) {
        setStatusFn("调整失败: " + (err?.message || err));
      }
      menu.classList.add("hidden");
    });
  });

  // 模型/舞蹈子菜单
  const modelSub = $("model-sub");
  const danceSub = $("dance-sub");

  async function _populateModelSub() {
    if (!modelSub) return;
    try {
      const list = await window.call("listModels");
      const names = list.map((p) => p.split(/[\\/]/).pop()).filter((n) => n);
      const cur = getCurrentModel() || "";
      const items = names.map((n) =>
        '<div class="sub-item' + (n === cur ? ' cur' : '') + '" data-model="' + n.replace(/"/g, "&quot;") + '">'
        + (n === cur ? '▶ ' : '') + n + '</div>'
      );
      items.push('<div class="sub-sep"></div>');
      items.push('<div class="sub-item" data-action="open-models-folder">📁 打开模型文件夹</div>');
      modelSub.innerHTML = items.join("");
    } catch (_) {}
  }

  async function _populateDanceSub() {
    if (!danceSub) return;
    try {
      const list = await window.call("listDances");
      const cur = getDanceName() ? getDanceName() + ".vmd" : "";
      const items = list.map((n) =>
        '<div class="sub-item' + (n === cur ? ' cur' : '') + '" data-dance="' + n.replace(/"/g, "&quot;") + '">'
        + (n === cur ? '▶ ' : '') + n + '</div>'
      );
      items.push('<div class="sub-sep"></div>');
      items.push('<div class="sub-item" data-action="open-dances-folder">📁 打开舞蹈文件夹</div>');
      danceSub.innerHTML = items.join("");
    } catch (_) {}
  }

  _populateModelSub();
  _populateDanceSub();

  modelSub?.addEventListener("click", async (ev) => {
    const item = ev.target.closest(".sub-item");
    if (!item) return;
    const name = item.dataset.model;
    if (name) {
      ev.stopPropagation();
      try {
        await loadVRM(await modelUrl(name), window.__scene, () => {
          if (!getDanceName()) loadDance(DEFAULT_DANCE, getVrm(), window.danceUrl);
        }, () => window.greetingBubble());
        setStatusFn("已切换: " + name);
      } catch (err) {
        setStatusFn("切换失败: " + (err?.message || err));
      }
      document.getElementById("pet-menu").classList.add("hidden");
    } else if (item.dataset.action === "open-models-folder") {
      ev.stopPropagation();
      try {
        const dir = await window.call("modelDir");
        if (dir) await window.call("openFolder", dir);
      } catch (_) {}
      document.getElementById("pet-menu").classList.add("hidden");
    }
  });

  danceSub?.addEventListener("click", async (ev) => {
    const item = ev.target.closest(".sub-item");
    if (!item) return;
    const name = item.dataset.dance;
    if (name) {
      ev.stopPropagation();
      const base = name.replace(/\.vmd$/i, "");
      loadDance(base, getVrm(), window.danceUrl);
      setStatusFn("已切换舞蹈: " + base);
      document.getElementById("pet-menu").classList.add("hidden");
    } else if (item.dataset.action === "open-dances-folder") {
      ev.stopPropagation();
      try {
        const dir = await window.call("danceDir");
        if (dir) await window.call("openFolder", dir);
      } catch (_) {}
      document.getElementById("pet-menu").classList.add("hidden");
    }
  });
}

export function initTauriEvents(setModeFn, setStatusFn) {
  window.whenTauriReady(() => {
    document.title = "3D Pet [ready]";
    window.__TAURI__.event.listen("toggle-mode", (e) => {
      document.title = "3D Pet [" + e.payload + "]";
      window.__TAURI__.event.emit("mode-confirmed", "main:" + e.payload);
      setModeFn(e.payload);
    });

    const DRAG_THRESHOLD = 20;
    let press = null;
    document.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      press = { x: e.clientX, y: e.clientY, moved: false };
    });
    document.addEventListener("mousemove", (e) => {
      if (!press || press.moved) return;
      if (Math.hypot(e.clientX - press.x, e.clientY - press.y) > DRAG_THRESHOLD) {
        press.moved = true;
        press = null;
        try { window.__TAURI__.window.getCurrentWindow().startDragging(); } catch (err) {}
      }
    });
    document.addEventListener("mouseup", () => { press = null; });

    // 滚轮缩放
    document.addEventListener("wheel", (e) => {
      if (e.target.closest(".submenu")) return;
      e.preventDefault();
      window.__viewZoom = Math.max(0.3, Math.min(4, window.__viewZoom * (e.deltaY > 0 ? 1.1 : 0.9)));
    }, { passive: false });

    window.__TAURI__.event.listen("tts", (e) => {
      playAudioFrom(window.__TAURI__.core.convertFileSrc(String(e.payload).replace(/\\/g, "/")));
    });
    window.__TAURI__.event.listen("tts-multi", (e) => {
      const paths = Array.isArray(e.payload) ? e.payload : [];
      const urls = paths.map((p) =>
        window.__TAURI__.core.convertFileSrc(String(p).replace(/\\/g, "/"))
      );
      startQueue(urls);
    });
    window.__TAURI__.event.listen("tts-error", (e) => {
      window.bubble(String(e.payload || "TTS 失败"), 2600);
    });
    window.__TAURI__.event.listen("stop-audio", () => {
      stopAudio();
      if (getVrm()) getVrm().expressionManager?.setValue("aa", 0);
      window.bubble("已停止朗读", 1600);
    });
    window.__TAURI__.event.listen("pause-audio", () => pauseAudio());
    window.__TAURI__.event.listen("resume-audio", () => resumeAudio());
  });
}