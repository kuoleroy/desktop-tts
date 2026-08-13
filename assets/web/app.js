import * as THREE from "three";
import { GLTFLoader } from "./vendor/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "./vendor/three-vrm.module.min.js";

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
      vrm: typeof vrm !== "undefined" && !!vrm,
      canvas: c ? c.width + "x" + c.height : null,
      gl,
      shot,
      ts: Date.now(),
    }),
  }).catch(() => {});
}, 3000);

const $ = (id) => document.getElementById(id);

const canvas = $("stage");
const renderer = new THREE.WebGLRenderer({
  canvas,
  alpha: true,
  antialias: true,
  preserveDrawingBuffer: true,
});
renderer.setClearColor(0x000000, 0);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 20);
scene.add(camera);

const light = new THREE.DirectionalLight(0xffffff, 1.6);
light.position.set(1, 1.5, 2);
scene.add(light);
const fill = new THREE.DirectionalLight(0xcfe8ff, 0.5);
fill.position.set(-1, -0.5, -1);
scene.add(fill);
scene.add(new THREE.AmbientLight(0xffffff, 0.35));

let vrm = null;
let currentModel = "";

const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));

async function loadVRM(url) {
  try {
    const gltf = await loader.loadAsync(url);
    const loaded = gltf.userData.vrm;
    VRMUtils.removeUnnecessaryVertices(loaded.scene);
    VRMUtils.combineSkeletons(loaded.scene);
    if (loaded.meta?.metaVersion === "0") {
      VRMUtils.rotateVRM0(loaded.scene);
    }
    loaded.scene.rotation.y = Math.PI;
    if (vrm) {
      scene.remove(vrm.scene);
      vrm.scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
          const mats = Array.isArray(o.material) ? o.material : [o.material];
          mats.forEach((m) => m.dispose());
        }
      });
    }
    vrm = loaded;
    scene.add(vrm.scene);
    armsDown = false;
    currentModel = url.split("/").pop();
    bubble(`你好，我是桌面小精灵，请多多关照！`, 2600);
  } catch (e) {
    setStatus("加载失败: " + e.message);
  }
}

async function modelUrl(name) {
  // Tauri 下用 asset 协议从根目录 models/ 加载；dev(8877)/浏览器 下走相对 models/
  // 注意：__TAURI__ 注入晚于顶层脚本，必须先等就绪，否则会误走浏览器相对路径
  const isTauri = await tauriReady();
  if (isTauri && window.__TAURI__?.core?.convertFileSrc) {
    const dir = await call("modelDir");
    return window.__TAURI__.core.convertFileSrc(
      dir.replace(/\\/g, "/") + "/" + name
    );
  }
  return "models/" + name;
}

// 等待 Tauri IPC 注入完成；浏览器/超时则返回 false
function tauriReady(timeout = 3000) {
  return new Promise((resolve) => {
    if (window.__TAURI__?.core) return resolve(true);
    const t = setInterval(() => {
      if (window.__TAURI__?.core) {
        clearInterval(t);
        resolve(true);
      }
    }, 50);
    setTimeout(() => {
      clearInterval(t);
      resolve(false);
    }, timeout);
  });
}

async function loadModelByIndex(i) {
  const list = await call("listModels");
  if (list && list[i]) {
    const name = list[i].split(/[\\/]/).pop();
    loadVRM(await modelUrl(name));
  }
}

let bubbleTimer = null;
function bubble(text, ms = 2800) {
  $("bubble-text").textContent = text;
  $("bubble").classList.remove("hidden");
  clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(() => $("bubble").classList.add("hidden"), ms);
}
window.bubble = bubble;

function setStatus(s) {
  $("status").textContent = s;
  $("status").style.display = "block";
  clearTimeout(setStatus._t);
  setStatus._t = setTimeout(() => ($("status").style.display = "none"), 3000);
}

function call(method, ...args) {
  const CMDS = {
    readText: ["read_text", (v) => ({ text: v })],
    stopRead: ["stop_read", () => undefined],
    setVoice: ["set_voice", (v) => ({ name: v })],
    setRate: ["set_rate", (v) => ({ rate: v })],
    setPitch: ["set_pitch", (v) => ({ pitch: v })],
    listModels: ["list_models", () => undefined],
    modelDir: ["model_dir", () => undefined],
    quit: ["quit", () => undefined],
  };
  if (window.__TAURI__?.core?.invoke && CMDS[method]) {
    const [cmd, wrap] = CMDS[method];
    return window.__TAURI__.core.invoke(cmd, wrap(args[0]));
  }
  const mocks = {
    listModels: () => [],
    modelDir: () => "",
    readText: () => {},
    stopRead: () => {},
  };
  return Promise.resolve(mocks[method] ? mocks[method](...args) : null);
}

/* ---- 待机动画：呼吸 + 微摆 + 眨眼 + 手臂放松 ---- */
let breathe = 0;
let blinkT = 3;
let nextBlink = 3 + Math.random() * 3;
let armsDown = false;

const ARM_DOWN = -1.25;
const ARM_SWING = 0.04;

function relaxArms(v) {
  const l = v.humanoid.getNormalizedBoneNode("leftUpperArm");
  const r = v.humanoid.getNormalizedBoneNode("rightUpperArm");
  if (l) l.rotation.z = ARM_DOWN;
  if (r) r.rotation.z = -ARM_DOWN;
  const ll = v.humanoid.getNormalizedBoneNode("leftLowerArm");
  const rl = v.humanoid.getNormalizedBoneNode("rightLowerArm");
  if (ll) ll.rotation.z = 0.25;
  if (rl) rl.rotation.z = -0.25;
}

function updateIdle(dt, t) {
  if (!vrm) return;
  if (!armsDown) {
    relaxArms(vrm);
    armsDown = true;
  }
  breathe += dt * 1.6;
  const b = Math.sin(breathe) * 0.015;
  const chest = vrm.humanoid.getNormalizedBoneNode("chest");
  if (chest) {
    chest.rotation.x = b;
    chest.rotation.z = Math.sin(t * 0.7) * 0.008;
  }
  const head = vrm.humanoid.getNormalizedBoneNode("head");
  if (head) {
    head.rotation.y = Math.sin(t * 0.4) * 0.03;
    head.rotation.z = Math.sin(t * 0.3 + 1) * 0.015;
  }
  const l = vrm.humanoid.getNormalizedBoneNode("leftUpperArm");
  const r = vrm.humanoid.getNormalizedBoneNode("rightUpperArm");
  if (l) l.rotation.z = ARM_DOWN + Math.sin(t * 1.6) * ARM_SWING;
  if (r) r.rotation.z = -ARM_DOWN - Math.sin(t * 1.6 + 0.4) * ARM_SWING;
  blinkT -= dt;
  if (blinkT < 0) {
    blinkT = Math.min(0.18, nextBlink);
    if (blinkT <= 0) {
      nextBlink = 2.5 + Math.random() * 3;
      blinkT = nextBlink;
    }
    const v = Math.max(0, Math.sin((nextBlink - blinkT) / 0.18 * Math.PI));
    vrm.expressionManager?.setValue("blink", v);
  }
  vrm.update(dt);
}

/* ---- 口型：WebAudio 音量 → mouthOpen ---- */
let audioCtx = null;
let analyser = null;
let audioSrc = null;
let mouthLevel = 0;

async function playAudioFrom(blobUrl) {
  stopAudio();
  audioCtx = audioCtx || new AudioContext();
  if (!audioCtx) {
    setStatus("AudioContext failed to initialize");
    return;
  }
  try {
    const res = await fetch(blobUrl);
    if (!res.ok) {
      setStatus("Failed to fetch audio: " + res.statusText);
      return;
    }
    const buf = await res.arrayBuffer();
    const audio = await audioCtx.decodeAudioData(buf);
    audioSrc = audioCtx.createBufferSource();
    audioSrc.buffer = audio;
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    audioSrc.connect(analyser);
    analyser.connect(audioCtx.destination);
    audioSrc.onended = () => {
      mouthLevel = 0;
      if (vrm) vrm.expressionManager?.setValue("aa", 0);
      broadcastPlayState("idle");
    };
    audioSrc.onerror = (e) => {
      setStatus("Audio playback error: " + e.message);
      broadcastPlayState("idle");
    };
    audioSrc.start();
    broadcastPlayState("playing");
  } catch (e) {
    setStatus("Audio playback failed: " + e.message);
    broadcastPlayState("idle");
  }
}
window.playAudioFrom = playAudioFrom;

// 播放状态：playing / paused / idle
function broadcastPlayState(state) {
  window.__TAURI__?.event?.emit("play-state", state);
}

function stopAudio() {
  if (audioSrc) {
    try { audioSrc.stop(); } catch (_) {}
    audioSrc.disconnect();
    audioSrc = null;
  }
  if (audioCtx && audioCtx.state === "suspended") {
    audioCtx.resume().catch(() => {});
  }
  broadcastPlayState("idle");
}
window.stopAudio = stopAudio;

// 暂停 / 恢复播放
function pauseAudio() {
  if (!audioCtx || !audioSrc) return;
  if (audioCtx.state === "running") {
    audioCtx.suspend().then(() => broadcastPlayState("paused")).catch(() => {});
  }
}
window.pauseAudio = pauseAudio;

function resumeAudio() {
  if (!audioCtx || !audioSrc) return;
  if (audioCtx.state === "suspended") {
    audioCtx.resume().then(() => broadcastPlayState("playing")).catch(() => {});
  }
}
window.resumeAudio = resumeAudio;

const freqData = new Uint8Array(128);
function updateMouth() {
  if (!analyser) return;
  analyser.getByteFrequencyData(freqData);
  let sum = 0;
  for (let i = 0; i < 128; i++) sum += freqData[i];
  const level = sum / 128 / 255;
  mouthLevel = mouthLevel * 0.6 + level * 0.4;
  const open = Math.min(1, mouthLevel * 4);
  vrm?.expressionManager?.setValue("aa", open);
}

/* ---- 主循环 ---- */
const clock = new THREE.Clock();
function renderLoop() {
  const dt = Math.min(clock.getDelta(), 0.1);
  const t = clock.elapsedTime;
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
    camera.position.set(center.x, center.y - size.y * 0.15, center.z + dist);
    camera.lookAt(center.x, center.y + size.y * 0.05, center.z);
  }
  updateIdle(dt, t);
  updateMouth();
  renderer.render(scene, camera);
  requestAnimationFrame(renderLoop);
}

/* ---- 自适应尺寸 ---- */
function resize() {
  const w = window.innerWidth || document.documentElement.clientWidth;
  const h = window.innerHeight || document.documentElement.clientHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}
window.addEventListener("resize", resize);
resize();

/* ---- 观赏/交互模式切换（纯 DOM，不动渲染） ---- */
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

/* ---- Tauri 事件（铁律 3：只监听，不碰窗口） ---- */
document.title = "3D Pet [booted]";
// Windows 上 __TAURI__ 注入晚于顶层脚本执行（tauri #12990），须等就绪
function whenTauriReady(cb) {
  if (window.__TAURI__?.event) return cb();
  const t = setInterval(() => {
    if (window.__TAURI__?.event) {
      clearInterval(t);
      cb();
    }
  }, 100);
  setTimeout(() => clearInterval(t), 5000);
}
whenTauriReady(() => {
  document.title = "3D Pet [ready]";
  window.__TAURI__.event.listen("toggle-mode", (e) => {
    document.title = "3D Pet [" + e.payload + "]";
    window.__TAURI__.event.emit("mode-confirmed", "main:" + e.payload);
    setMode(e.payload);
  });
  window.__TAURI__.event.listen("tts", (e) => {
    // 路径含反斜杠需归一化为正斜杠，否则 asset 协议 URL 解析失败
    playAudioFrom(window.__TAURI__.core.convertFileSrc(String(e.payload).replace(/\\/g, "/")));
  });
  window.__TAURI__.event.listen("tts-error", (e) => {
    bubble(String(e.payload || "TTS 失败"), 2600);
  });
  // 面板「停止」→ 真实停止本窗口音频
  window.__TAURI__.event.listen("stop-audio", () => {
    stopAudio();
    if (vrm) vrm.expressionManager?.setValue("aa", 0);
    bubble("已停止朗读", 1600);
  });
  // 面板「暂停/开始」→ 暂停/恢复本窗口音频
  window.__TAURI__.event.listen("pause-audio", () => pauseAudio());
  window.__TAURI__.event.listen("resume-audio", () => resumeAudio());
});

/* ---- 进入交互模式时初始化控件 ---- */
setMode("watch");

modelUrl("AliciaSolid.vrm").then((u) => loadVRM(u));
renderLoop();