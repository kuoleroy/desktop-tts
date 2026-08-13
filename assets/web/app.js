import * as THREE from "three";
import { GLTFLoader } from "./vendor/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "./vendor/three-vrm.module.min.js";
import { parseVMD } from "./vmd2vrm.js";

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
    // 加载默认舞蹈（VMD 动作），供静置时播放
    if (!danceName) loadDance(DEFAULT_DANCE);
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

// ---- VMD 舞蹈播放器 ----
// 静置时播放 VMD 舞蹈动作（真实动作），朗读时暂停。基于 vmd2vrm 转换的骨骼动画。
let danceClips = null;          // 解析后的舞蹈动画集 [{name,type,times,values}]
let danceDuration = 0;
let danceTime = 0;              // 舞蹈播放进度（秒，循环）
let danceName = "";             // 当前舞蹈标识
const DEFAULT_DANCE = "5";      // 默认静置舞蹈文件（assets/dance/5.vmd）
const danceIndex = {};          // boneName -> {rot:{times,values}} 快速查找
const rotCache = new Map();     // 缓存 Quaternion 避免重复分配

// 把转换结果建成便于按时间采样索引
function buildDanceIndex(clips) {
  const idx = {};
  for (const tl of clips) {
    if (tl.type !== "rotation") continue;
    idx[tl.name] = { times: tl.times, values: tl.values };
  }
  return idx;
}

// 采样：返回该骨骼在 t 秒处的四元数（线性插值）
const _qA = new THREE.Quaternion();
const _qB = new THREE.Quaternion();
const _qOut = new THREE.Quaternion();
function sampleRotation(boneIdx, t) {
  const tl = danceIndex[boneIdx];
  if (!tl) return null;
  const { times, values } = tl;
  const n = times.length;
  if (!n) return null;
  if (t <= times[0]) { _qOut.fromArray(values, 0); return _qOut; }
  if (t >= times[n - 1]) { _qOut.fromArray(values, (n - 1) * 4); return _qOut; }
  // 二分定位
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (times[mid] <= t) lo = mid; else hi = mid; }
  const t0 = times[lo], t1 = times[hi];
  const v = (t1 === t0) ? 0 : (t - t0) / (t1 - t0);
  _qA.fromArray(values, lo * 4);
  _qB.fromArray(values, hi * 4);
  _qOut.copy(_qA).slerp(_qB, v);
  return _qOut;
}

async function loadDance(name) {
  try {
    const res = await fetch("dance/" + name + ".vmd");
    if (!res.ok) throw new Error("fetch " + res.status);
    const buf = await res.arrayBuffer();
    const parsed = parseVMD(buf);
    danceClips = parsed.timelines;
    danceDuration = parsed.duration || 1;
    Object.keys(danceIndex).forEach((k) => delete danceIndex[k]);
    Object.assign(danceIndex, buildDanceIndex(danceClips));
    danceName = name;
    danceTime = 0;
    recordBoneRestPose();
    console.log("[dance] loaded", name, "duration", danceDuration.toFixed(1), "s");
  } catch (e) {
    console.warn("[dance] load failed", name, e);
  }
}

// 重置舞蹈驱动的骨骼到默认（rest）姿态，朗读时恢复自然站姿
let dancePoseApplied = false;
const boneRestPose = {}; // boneName -> 初始 quaternion（加载模型时记录）
function recordBoneRestPose() {
  if (!vrm) return;
  for (const boneName of Object.keys(danceIndex)) {
    const node = vrm.humanoid.getNormalizedBoneNode(boneName);
    if (node) boneRestPose[boneName] = node.quaternion.clone();
  }
}
function resetDancePose() {
  if (!vrm || !dancePoseApplied) return;
  for (const boneName of Object.keys(danceIndex)) {
    const node = vrm.humanoid.getNormalizedBoneNode(boneName);
    if (node) node.quaternion.copy(boneRestPose[boneName] || new THREE.Quaternion());
  }
  dancePoseApplied = false;
}

function updateDance(dt, t) {
  if (!vrm) return;
  if (!danceClips || !danceDuration) { updateDanceSine(dt, t); return; }
  danceTime += dt;
  if (danceTime > danceDuration) danceTime -= danceDuration; // 循环
  for (const boneName of Object.keys(danceIndex)) {
    const q = sampleRotation(boneName, danceTime);
    if (!q) continue;
    const node = vrm.humanoid.getNormalizedBoneNode(boneName);
    if (node) { node.quaternion.copy(q); dancePoseApplied = true; }
  }
}

// 正弦回退动画：VMD 未加载时用的简单动作
let dancePhase = 0;
function updateDanceSine(dt, t) {
  if (!vrm) return;
  dancePhase += dt * 2.2;
  const h = vrm.humanoid;
  const ph = dancePhase;
  const get = (n) => h.getNormalizedBoneNode(n);
  const hips = get("hips");
  if (hips) {
    hips.rotation.z = Math.sin(ph) * 0.08;
    hips.rotation.y = Math.sin(ph * 0.5) * 0.12;
    hips.rotation.x = Math.sin(ph * 0.5 + 0.5) * 0.05;
  }
  const chest = get("chest");
  if (chest) {
    chest.rotation.y = -Math.sin(ph * 0.5) * 0.15;
    chest.rotation.z = Math.sin(ph) * 0.04;
    chest.rotation.x = Math.sin(ph + 0.3) * 0.03;
  }
  const lu = get("leftUpperArm"), ru = get("rightUpperArm");
  if (lu) lu.rotation.z = -Math.PI * 0.55 + Math.sin(ph * 2) * 0.25;
  if (ru) ru.rotation.z = Math.PI * 0.55 + Math.sin(ph * 2 + Math.PI) * 0.25;
  const ll = get("leftLowerArm"), rl = get("rightLowerArm");
  if (ll) ll.rotation.z = -0.4 + Math.sin(ph * 2 + 0.5) * 0.3;
  if (rl) rl.rotation.z = 0.4 + Math.sin(ph * 2 + 1) * 0.3;
  const luL = get("leftUpperLeg"), ruL = get("rightUpperLeg");
  if (luL) luL.rotation.x = Math.max(0, Math.sin(ph)) * 0.5;
  if (ruL) ruL.rotation.x = Math.max(0, Math.sin(ph + Math.PI)) * 0.5;
  const llo = get("leftLowerLeg"), rlo = get("rightLowerLeg");
  if (llo) llo.rotation.x = Math.max(0, Math.sin(ph)) * 0.35;
  if (rlo) rlo.rotation.x = Math.max(0, Math.sin(ph + Math.PI)) * 0.35;
  const head = get("head");
  if (head) {
    head.rotation.z = Math.sin(ph) * 0.1;
    head.rotation.y = Math.sin(ph * 0.5) * 0.1;
  }
}

function updateIdle(dt, t) {
  if (!vrm) return;
  // 始终跳舞（不区分是否朗读）
  updateDance(dt, t);
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
      audioSrc = null;
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

// ---- 长文本分块朗读队列：依次播放多个音频文件，播完自动下一个 ----
let playQueue = [];
let playingId = 0;
let queuePaused = false;
let totalBlocks = 0;
let currentBlock = 0;

function playQueueItem(idx) {
  if (queuePaused) {
    // 已暂停：不再自动播下一块，保持当前音频（若存在）停在暂停态
    return;
  }
  if (!playQueue[idx]) {
    // 播完整个队列
    playQueue = [];
    totalBlocks = 0;
    currentBlock = 0;
    broadcastProgress(1, 0);
    broadcastPlayState("idle");
    return;
  }
  currentBlock = idx;
  playingId++;
  const myId = playingId;
  const url = playQueue[idx];
  const onDone = () => {
    if (myId !== playingId) return; // 已被停止/替换
    playQueueItem(idx + 1);
  };
  playSequence(url, onDone, idx);
}

function playSequence(url, onDone, blockIdx) {
  stopAudio(true); // 停止但不广播 idle（由队列接管）
  audioCtx = audioCtx || new AudioContext();
  if (!audioCtx) {
    onDone();
    return;
  }
  (async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        setStatus("播放失败: " + res.statusText);
        onDone();
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
        audioSrc = null;
        onDone();
      };
      audioSrc.onerror = () => { audioSrc = null; onDone(); };
      audioSrc.start();
      // 每块播放中定期更新进度（当前块内部时间 / 总时长）
      startProgressTimer(blockIdx);
      broadcastPlayState("playing");
    } catch (e) {
      setStatus("播放失败: " + e.message);
      onDone();
    }
  })();
}

// 进度定时器：按 audioCtx.currentTime 与当前块 buffer 时长计算
let progressTimer = null;
function startProgressTimer(blockIdx) {
  clearInterval(progressTimer);
  progressTimer = setInterval(() => {
    if (!audioSrc || !audioCtx) {
      broadcastProgress(blockIdx / Math.max(1, totalBlocks), 0);
      return;
    }
    const dur = audioSrc.buffer ? audioSrc.buffer.duration : 0;
    const cur = audioCtx.currentTime || 0;
    const blockFrac = dur > 0 ? Math.min(1, cur / dur) : 0;
    const frac = (blockIdx + blockFrac) / Math.max(1, totalBlocks);
    broadcastProgress(frac, cur);
  }, 500);
}

function stopProgressTimer() {
  clearInterval(progressTimer);
  progressTimer = null;
}

// 进度事件：整体比例 + 当前块内秒数
function broadcastProgress(frac, sec) {
  window.__TAURI__?.event?.emit("read-progress", { frac, sec });
}

// 停止队列（广播 idle）
function stopQueue() {
  playingId++;
  queuePaused = false;
  playQueue = [];
  totalBlocks = 0;
  currentBlock = 0;
  stopProgressTimer();
  if (audioSrc) {
    try { audioSrc.stop(); } catch (_) {}
    audioSrc.disconnect();
    audioSrc = null;
  }
  broadcastProgress(0, 0);
  broadcastPlayState("idle");
}

function startQueue(paths) {
  stopQueue();
  queuePaused = false;
  playQueue = paths.slice();
  totalBlocks = playQueue.length;
  currentBlock = 0;
  broadcastProgress(0, 0);
  playQueueItem(0);
}

// 播放状态：playing / paused / idle
function broadcastPlayState(state) {
  window.__TAURI__?.event?.emit("play-state", state);
}

function stopAudio(silent) {
  if (audioSrc) {
    try { audioSrc.stop(); } catch (_) {}
    audioSrc.disconnect();
    audioSrc = null;
  }
  if (audioCtx && audioCtx.state === "suspended") {
    audioCtx.resume().catch(() => {});
  }
  if (!silent) broadcastPlayState("idle");
}
window.stopAudio = stopAudio;

// 暂停 / 恢复播放（队列感知：跨块保持暂停态）
function pauseAudio() {
  queuePaused = true;
  if (!audioCtx || !audioSrc) {
    // 无当前音频但队列未播完 → 也要让面板进入暂停态
    broadcastPlayState("paused");
    return;
  }
  if (audioCtx.state === "running") {
    audioCtx.suspend().then(() => broadcastPlayState("paused")).catch(() => {});
  } else {
    broadcastPlayState("paused");
  }
}
window.pauseAudio = pauseAudio;

function resumeAudio() {
  queuePaused = false;
  if (!audioCtx || !audioSrc) {
    // 暂停发生在块间隙：恢复时若队列还有剩余块，继续播
    if (playQueue.length) {
      playQueueItem(0);
    } else {
      broadcastPlayState("idle");
    }
    return;
  }
  if (audioCtx.state === "suspended") {
    audioCtx.resume().then(() => broadcastPlayState("playing")).catch(() => {});
  } else {
    broadcastPlayState("playing");
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
const viewRot = { x: 0, y: 0 };
let viewZoom = 1; // 滚轮缩放
const _up = new THREE.Vector3(0, 1, 0);
const _rightAxis = new THREE.Vector3(1, 0, 0);
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
    // 先算基准相机位（绕 center），再叠加右键视角旋转
    const base = new THREE.Vector3(center.x, center.y - size.y * 0.15, center.z + dist * viewZoom);
    camera.position.copy(base).sub(center);
    camera.position.applyAxisAngle(_up, viewRot.y).applyAxisAngle(_rightAxis, viewRot.x);
    camera.position.add(center);
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
  // 双击模型 → 显示面板（回到面板）。由 Rust 处理显示并切交互模式。
  document.addEventListener("dblclick", () => {
    window.__TAURI__.event.emit("pet-dblclick", {});
  });
  // 模型拖动：位移超过阈值才拖动窗口（可交互模式生效），避免与双击冲突
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
      try {
        window.__TAURI__.window.getCurrentWindow().startDragging();
      } catch (err) {
        /* 穿透/预览时无 Tauri 拖动，忽略 */
      }
    }
  });
  document.addEventListener("mouseup", () => { press = null; });
  // 右键：按下未拖动 → 弹菜单；拖动 → 视角水平+垂直旋转（轨道相机）
  const menu = document.getElementById("pet-menu");
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
    }
    rightDrag.on = false;
  });
  document.addEventListener("mouseleave", () => { rightDrag.on = false; });
  document.addEventListener("click", (e) => {
    if (menu && !menu.contains(e.target)) menu.classList.add("hidden");
  });
  menu?.querySelectorAll(".menu-item").forEach((item) => {
    item.addEventListener("click", async () => {
      const a = item.dataset.action;
      if (a === "reset-size") {
        try {
          const win = window.__TAURI__.window.getCurrentWindow();
          const W = window.__TAURI__.window;
          const size = new W.PhysicalSize(240, 300);
          await win.setSize(size);
          setStatus("已恢复默认大小");
        } catch (err) {
          setStatus("恢复失败: " + (err?.message || err));
        }
      }
      menu.classList.add("hidden");
    });
  });
  // 右下角缩放手柄 → 系统 resize 拖拽
  document.getElementById("resize-handle")?.addEventListener("mousedown", (e) => {
    e.preventDefault();
    try {
      window.__TAURI__.window.getCurrentWindow().startResizeDrag(
        window.__TAURI__.window.WindowResizeEdge.BottomRight
      );
    } catch (_) {}
  });
  // 滚轮缩放视角（模型放大缩小）
  document.addEventListener("wheel", (e) => {
    e.preventDefault();
    viewZoom = Math.max(0.3, Math.min(4, viewZoom * (e.deltaY > 0 ? 1.1 : 0.9)));
  }, { passive: false });
  window.__TAURI__.event.listen("tts", (e) => {
    // 路径含反斜杠需归一化为正斜杠，否则 asset 协议 URL 解析失败
    playAudioFrom(window.__TAURI__.core.convertFileSrc(String(e.payload).replace(/\\/g, "/")));
  });
  // 分块朗读：多文件路径，排队顺序播放
  window.__TAURI__.event.listen("tts-multi", (e) => {
    const paths = Array.isArray(e.payload) ? e.payload : [];
    const urls = paths.map((p) =>
      window.__TAURI__.core.convertFileSrc(String(p).replace(/\\/g, "/"))
    );
    startQueue(urls);
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