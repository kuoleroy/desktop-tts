// 舞蹈动画模块（VMD + 正弦回退）
import * as THREE from "three";
import { parseVMD } from "./vmd2vrm.js";

let danceClips = null;
let danceDuration = 0;
let danceTime = 0;
let danceName = "";
const DEFAULT_DANCE = "5";
const danceIndex = {};  // boneName -> {rot:{times,values}}
let dancePoseApplied = false;
const boneRestPose = {};

const _qA = new THREE.Quaternion();
const _qB = new THREE.Quaternion();
const _qOut = new THREE.Quaternion();

export function getDanceName() { return danceName; }
export function getDancePoseApplied() { return dancePoseApplied; }
export function getDanceIndex() { return danceIndex; }
export function getBoneRestPose() { return boneRestPose; }
export { DEFAULT_DANCE };

function buildDanceIndex(clips) {
  const idx = {};
  for (const tl of clips) {
    if (tl.type !== "rotation") continue;
    idx[tl.name] = { times: tl.times, values: tl.values };
  }
  return idx;
}

function sampleRotation(boneIdx, t) {
  const tl = danceIndex[boneIdx];
  if (!tl) return null;
  const { times, values } = tl;
  const n = times.length;
  if (!n) return null;
  if (t <= times[0]) { _qOut.fromArray(values, 0); return _qOut; }
  if (t >= times[n - 1]) { _qOut.fromArray(values, (n - 1) * 4); return _qOut; }
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (times[mid] <= t) lo = mid; else hi = mid; }
  const t0 = times[lo], t1 = times[hi];
  const v = (t1 === t0) ? 0 : (t - t0) / (t1 - t0);
  _qA.fromArray(values, lo * 4);
  _qB.fromArray(values, hi * 4);
  _qOut.copy(_qA).slerp(_qB, v);
  return _qOut;
}

export async function loadDance(name, vrm, danceUrlFn) {
  try {
    const url = await danceUrlFn(name + ".vmd");
    const res = await fetch(url);
    if (!res.ok) throw new Error("fetch " + res.status);
    const buf = await res.arrayBuffer();
    const parsed = parseVMD(buf);
    danceClips = parsed.timelines;
    danceDuration = parsed.duration || 1;
    Object.keys(danceIndex).forEach((k) => delete danceIndex[k]);
    Object.assign(danceIndex, buildDanceIndex(danceClips));
    danceName = name;
    danceTime = 0;
    recordBoneRestPose(vrm);
    console.log("[dance] loaded", name, "duration", danceDuration.toFixed(1), "s");
  } catch (e) {
    console.warn("[dance] load failed", name, e);
  }
}

function recordBoneRestPose(vrm) {
  if (!vrm) return;
  for (const boneName of Object.keys(danceIndex)) {
    const node = vrm.humanoid.getNormalizedBoneNode(boneName);
    if (node) boneRestPose[boneName] = node.quaternion.clone();
  }
}

export function resetDancePose(vrm) {
  if (!vrm || !dancePoseApplied) return;
  for (const boneName of Object.keys(danceIndex)) {
    const node = vrm.humanoid.getNormalizedBoneNode(boneName);
    if (node) node.quaternion.copy(boneRestPose[boneName] || new THREE.Quaternion());
  }
  dancePoseApplied = false;
}

export function updateDance(dt, vrm) {
  if (!vrm) return;
  if (!danceClips || !danceDuration) { updateDanceSine(dt, vrm); return; }
  danceTime += dt;
  if (danceTime > danceDuration) danceTime -= danceDuration;
  for (const boneName of Object.keys(danceIndex)) {
    const q = sampleRotation(boneName, danceTime);
    if (!q) continue;
    const node = vrm.humanoid.getNormalizedBoneNode(boneName);
    if (node) { node.quaternion.copy(q); dancePoseApplied = true; }
  }
}

let dancePhase = 0;
function updateDanceSine(dt, vrm) {
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

export function updateIdle(dt, vrm, blinkT, nextBlink) {
  if (!vrm) return;
  updateDance(dt, vrm);
  blinkT.value -= dt;
  if (blinkT.value < 0) {
    blinkT.value = Math.min(0.18, nextBlink.value);
    if (blinkT.value <= 0) {
      nextBlink.value = 2.5 + Math.random() * 3;
      blinkT.value = nextBlink.value;
    }
    const v = Math.max(0, Math.sin((nextBlink.value - blinkT.value) / 0.18 * Math.PI));
    vrm.expressionManager?.setValue("blink", v);
  }
  vrm.update(dt);
}