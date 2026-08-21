// 主入口：3D 渲染循环 + 初始化
import * as THREE from "three";
import { loadVRM, modelUrl, getVrm, initLoader } from "./app-model.js";
import { updateIdle, loadDance, getDanceName, DEFAULT_DANCE } from "./app-dance.js";
import { updateMouth, getAudioCtx, getAnalyser } from "./app-audio.js";
import { initMenu, initTauriEvents } from "./app-menu.js";

// ---- 诊断 ----
window.__errs = [];
window.addEventListener("error", (e) => {
  window.__errs.push("error: " + e.message + " @ " + (e.filename || "") + ":" + (e.lineno || ""));
  console.error("[captured]", e.message);
});
window.addEventListener("unhandledrejection", (e) => {
  window.__errs.push("rejection: " + (e.reason && e.reason.message ? e.reason.message : String(e.reason)));
  console.error("[captured rejection]", e.reason);
});

let diagTick = 0;
setInterval(() => {
  const c = document.getElementById("stage");
  let gl = false;
  try {
    gl = !!(c && (c.getContext("webgl2") || c.getContext("webgl")));
  } catch (_) {}
  let shot = null;
  diagTick++;
  if (diagTick % 2 === 0 && c) {
    try {
      shot = c.toDataURL("image/png").slice(0, 200000);
    } catch (_) {}
  }
  fetch("/diag", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      errs: window.__errs.slice(-8),
      vrm: typeof getVrm !== "undefined" && !!getVrm(),
      canvas: c ? c.width + "x" + c.height : null,
      gl, shot, ts: Date.now(),
    }),
  }).catch(() => {});
}, 3000);

// ---- 公用工具 ----
const $ = (id) => document.getElementById(id);

window.tauriReady = function tauriReady(timeout = 3000) {
  return new Promise((resolve) => {
    if (window.__TAURI__?.core) return resolve(true);
    const t = setInterval(() => {
      if (window.__TAURI__?.core) { clearInterval(t); resolve(true); }
    }, 50);
    setTimeout(() => { clearInterval(t); resolve(false); }, timeout);
  });
};

window.whenTauriReady = function(cb) {
  if (window.__TAURI__?.event) return cb();
  const t = setInterval(() => {
    if (window.__TAURI__?.event) { clearInterval(t); cb(); }
  }, 100);
  setTimeout(() => clearInterval(t), 5000);
};

window.call = function(method, ...args) {
  const CMDS = {
    readText: ["read_text", (v) => ({ text: v })],
    stopRead: ["stop_read", () => undefined],
    setVoice: ["set_voice", (v) => ({ name: v })],
    setRate: ["set_rate", (v) => ({ rate: v })],
    setPitch: ["set_pitch", (v) => ({ pitch: v })],
    listModels: ["list_models", () => undefined],
    modelDir: ["model_dir", () => undefined],
    listDances: ["list_dances", () => undefined],
    modelsDirPath: ["model_dir", () => undefined],
    openFolder: ["open_folder", (v) => ({ path: v })],
    danceDir: ["dance_dir", () => undefined],
    quit: ["quit", () => undefined],
  };
  if (window.__TAURI__?.core?.invoke && CMDS[method]) {
    const [cmd, wrap] = CMDS[method];
    return window.__TAURI__.core.invoke(cmd, wrap(args[0]));
  }
  const mocks = {
    listModels: () => [], modelDir: () => "", listDances: () => [],
    openFolder: () => {}, danceDir: () => "",
    readText: () => {}, stopRead: () => {},
  };
  return Promise.resolve(mocks[method] ? mocks[method](...args) : null);
};

window.danceUrl = async function danceUrl(name) {
  const isTauri = await window.tauriReady();
  if (isTauri && window.__TAURI__?.core?.convertFileSrc) {
    const dir = await window.call("danceDir");
    return window.__TAURI__.core.convertFileSrc(dir.replace(/\\/g, "/") + "/" + name);
  }
  return "dance/" + name;
};

// ---- 气泡提示 ----
let bubbleTimer = null;
window.bubble = function bubble(text, ms = 2800) {
  $("bubble-text").textContent = text;
  $("bubble").classList.remove("hidden");
  clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(() => $("bubble").classList.add("hidden"), ms);
};

window.setStatus = function setStatus(s) {
  $("status").textContent = s;
  $("status").style.display = "block";
  clearTimeout(setStatus._t);
  setStatus._t = setTimeout(() => ($("status").style.display = "none"), 3000);
};

window.greetingBubble = function greetingBubble() {
  (async () => {
    let g = "你好，我是桌面小精灵，请多多关照！";
    try {
      if (window.__TAURI__?.core?.invoke) {
        const s = await window.__TAURI__.core.invoke("get_app_settings");
        if (s && s.greeting && String(s.greeting).trim()) g = String(s.greeting).trim();
      }
    } catch (_) {}
    window.bubble(g, 2600);
  })();
};

// ---- 场景初始化 ----
const canvas = $("stage");
const renderer = new THREE.WebGLRenderer({
  canvas, alpha: true, antialias: true, preserveDrawingBuffer: true,
});
renderer.setClearColor(0x000000, 0);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

const scene = new THREE.Scene();
window.__scene = scene;

const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 20);
scene.add(camera);

const light = new THREE.DirectionalLight(0xffffff, 1.6);
light.position.set(1, 1.5, 2);
scene.add(light);
const fill = new THREE.DirectionalLight(0xcfe8ff, 0.5);
fill.position.set(-1, -0.5, -1);
scene.add(fill);
scene.add(new THREE.AmbientLight(0xffffff, 0.35));

// ---- 加载器初始化 ----
initLoader();

// ---- 主循环 ----
const clock = new THREE.Clock();
const viewRot = { x: 0, y: 0 };
window.__viewZoom = 1;
const _up = new THREE.Vector3(0, 1, 0);
const _rightAxis = new THREE.Vector3(1, 0, 0);
let blinkT = { value: 3 };
let nextBlink = { value: 3 + Math.random() * 3 };

function renderLoop() {
  const dt = Math.min(clock.getDelta(), 0.1);
  const t = clock.elapsedTime;
  const vrm = getVrm();
  if (vrm) {
    const box = new THREE.Box3().setFromObject(vrm.scene);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const a = (camera.fov * Math.PI) / 360;
    const b = Math.atan(Math.tan(a) * camera.aspect);
    const ratio = 0.85;
    const dist = Math.max(
      size.y / (2 * ratio * Math.tan(a)),
      size.x / (2 * ratio * Math.tan(b)),
      size.z / (2 * ratio * Math.tan(a))
    );
    const base = new THREE.Vector3(center.x, center.y - size.y * 0.15, center.z + dist * window.__viewZoom);
    camera.position.copy(base).sub(center);
    camera.position.applyAxisAngle(_up, viewRot.y).applyAxisAngle(_rightAxis, viewRot.x);
    camera.position.add(center);
    camera.lookAt(center.x, center.y + size.y * 0.05, center.z);
  }
  updateIdle(dt, vrm, blinkT, nextBlink);
  updateMouth();
  renderer.render(scene, camera);
  requestAnimationFrame(renderLoop);
}

function resize() {
  const w = window.innerWidth || document.documentElement.clientWidth;
  const h = window.innerHeight || document.documentElement.clientHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}
window.addEventListener("resize", resize);
resize();

// ---- 模式切换 ----
let currentMode = "watch";
function setMode(mode) {
  currentMode = mode;
  const stage = $("stage");
  if (mode === "interact") {
    stage.classList.add("model-hidden");
  } else {
    stage.classList.remove("model-hidden");
  }
}
window.setMode = setMode;

// ---- 初始化 ----
document.title = "3D Pet [booted]";
initMenu(viewRot, { get value() { return window.__viewZoom; }, set value(v) { window.__viewZoom = v; } }, window.setStatus);
initTauriEvents(setMode, window.setStatus);
setMode("watch");

// 加载默认模型
modelUrl("AliciaSolid.vrm").then((u) => loadVRM(u, scene, () => {
  if (!getDanceName()) loadDance(DEFAULT_DANCE, getVrm(), window.danceUrl);
}, window.greetingBubble));

renderLoop();