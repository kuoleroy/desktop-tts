// VMD → VRM 骨骼动画转换
// 移植自 JLChnToZ/vrm-dance-viewer (MIT) 的 vmd2vrmanim.ts，
// 保留核心骨骼映射与坐标系转换，输出可直接驱动 VRM Humanoid 骨骼的动画数据。
import { Vector3, Quaternion, MathUtils } from "./vendor/three.module.min.js";
import { MMDParser } from "./vendor/mmd-parser.module.js";

const tempV3 = new Vector3();
const tempQ = new Quaternion();

const B = {
  Root: "全ての親",
  Center: "センター",
  Hips: "下半身",
  Spine: "上半身",
  Chest: "上半身2",
  Neck: "首",
  Head: "頭",
  LeftEye: "左目",
  LeftShoulder: "左肩",
  LeftUpperArm: "左腕",
  LeftLowerArm: "左ひじ",
  LeftHand: "左手首",
  LeftUpperLeg: "左足",
  LeftLowerLeg: "左ひざ",
  LeftFoot: "左足首",
  LeftFootIK: "左足ＩＫ",
  LeftToes: "左つま先",
  LeftToeIK: "左つま先ＩＫ",
  RightEye: "右目",
  RightShoulder: "右肩",
  RightUpperArm: "右腕",
  RightLowerArm: "右ひじ",
  RightHand: "右手首",
  RightUpperLeg: "右足",
  RightLowerLeg: "右ひざ",
  RightFoot: "右足首",
  RightFootIK: "右足ＩＫ",
  RightToes: "右つま先",
  RightToeIK: "右つま先ＩＫ",
};

const BONE_MAP = {
  [B.Hips]: "hips",
  [B.Spine]: "spine",
  [B.Chest]: "chest",
  [B.Neck]: "neck",
  [B.Head]: "head",
  [B.LeftEye]: "leftEye",
  [B.LeftShoulder]: "leftShoulder",
  [B.LeftUpperArm]: "leftUpperArm",
  [B.LeftLowerArm]: "leftLowerArm",
  [B.LeftHand]: "leftHand",
  [B.LeftUpperLeg]: "leftUpperLeg",
  [B.LeftLowerLeg]: "leftLowerLeg",
  [B.LeftFoot]: "leftFoot",
  [B.LeftToes]: "leftToes",
  [B.RightEye]: "rightEye",
  [B.RightShoulder]: "rightShoulder",
  [B.RightUpperArm]: "rightUpperArm",
  [B.RightLowerArm]: "rightLowerArm",
  [B.RightHand]: "rightHand",
  [B.RightUpperLeg]: "rightUpperLeg",
  [B.RightLowerLeg]: "rightLowerLeg",
  [B.RightFoot]: "rightFoot",
  [B.RightToes]: "rightToes",
};

// 手指骨骼：MMD 名 → VRM 名（thumb/index/middle/ring/little 三段）
const FINGER_MAP = {
  "左親指０": "leftThumbProximal", "左親指１": "leftThumbMetacarpal", "左親指２": "leftThumbDistal",
  "左人指１": "leftIndexProximal", "左人指２": "leftIndexIntermediate", "左人指３": "leftIndexDistal",
  "左中指１": "leftMiddleProximal", "左中指２": "leftMiddleIntermediate", "左中指３": "leftMiddleDistal",
  "左薬指１": "leftRingProximal", "左薬指２": "leftRingIntermediate", "左薬指３": "leftRingDistal",
  "左小指１": "leftLittleProximal", "左小指２": "leftLittleIntermediate", "左小指３": "leftLittleDistal",
  "右親指０": "rightThumbProximal", "右親指１": "rightThumbMetacarpal", "右親指２": "rightThumbDistal",
  "右人指１": "rightIndexProximal", "右人指２": "rightIndexIntermediate", "右人指３": "rightIndexDistal",
  "右中指１": "rightMiddleProximal", "右中指２": "rightMiddleIntermediate", "右中指３": "rightMiddleDistal",
  "右薬指１": "rightRingProximal", "右薬指２": "rightRingIntermediate", "右薬指３": "rightRingDistal",
  "右小指１": "rightLittleProximal", "右小指２": "rightLittleIntermediate", "右小指３": "rightLittleDistal",
};

const IK_MAP = {
  [B.LeftFootIK]: "leftFoot",
  [B.LeftToeIK]: "leftToes",
  [B.RightFootIK]: "rightFoot",
  [B.RightToeIK]: "rightToes",
};

const MORPH_MAP = {
  まばたき: "blink",
  ウィンク: "blinkLeft",
  ウィンク右: "blinkRight",
  あ: "aa",
  い: "ih",
  う: "ou",
  え: "ee",
  お: "oh",
};

const Z_30_CW = new Quaternion().setFromAxisAngle(new Vector3(0, 0, 1), 30 * MathUtils.DEG2RAD);
const Z_30_CCW = Z_30_CW.clone().invert();

// 输出：{ duration, timelines: [ {name, type:'rotation'|'position'|'morph', times:[], values:[]} ] }
export function parseVMD(buffer, vrmOffset = {}) {
  const vmd = new MMDParser.Parser().parseVmd(buffer);
  const morphs = convertMorphs(vmd.morphs || []);
  const motions = convertMotions(vmd.motions || [], vrmOffset);
  return {
    duration: Math.max(morphs.duration, motions.duration),
    timelines: [].concat(morphs.timelines, motions.timelines),
  };
}

function convertMorphs(morphs) {
  morphs.sort((a, b) => a.frameNum - b.frameNum);
  const tlMap = new Map();
  for (const { morphName, weight, frameNum } of morphs) {
    const name = MORPH_MAP[morphName];
    if (!name) continue;
    let tl = tlMap.get(name);
    if (!tl) tlMap.set(name, (tl = { name, type: "morph", times: [], values: [] }));
    const time = frameNum / 30;
    const i = tl.times.indexOf(time);
    if (i < 0) { tl.times.push(time); tl.values.push(weight); }
    else tl.values[i] = Math.max(tl.values[i], weight);
  }
  return { timelines: [...tlMap.values()], duration: lastFrame(morphs) / 30 };
}

function convertMotions(motions, vrmOffset) {
  motions.sort((a, b) => a.frameNum - b.frameNum);
  const tlMap = new Map();
  for (const name of Object.keys(BONE_MAP).concat(Object.keys(IK_MAP), Object.keys(FINGER_MAP), [B.Root, B.Center])) {
    tlMap.set(name, []);
  }
  for (const { boneName, frameNum, position, rotation } of motions) {
    const list = tlMap.get(boneName);
    if (list) list.push({
      boneName, frameNum,
      position: new Vector3().fromArray(position),
      rotation: new Quaternion().fromArray(rotation),
    });
  }
  fixPositions(tlMap, vrmOffset);
  const timelines = [];
  for (const [boneName, timeline] of tlMap) {
    let name = BONE_MAP[boneName] || FINGER_MAP[boneName];
    let isIK = false;
    if (!name) { isIK = !!IK_MAP[boneName]; name = IK_MAP[boneName]; }
    if (!name) continue;
    const times = [], rotations = [], positions = [];
    for (const f of timeline) {
      const i = times.push(f.frameNum / 30) - 1;
      f.rotation.toArray(rotations, i * 4);
      f.position.toArray(positions, i * 3);
    }
    if (!times.length) continue;
    timelines.push({ name, type: "rotation", isIK, times, values: rotations });
    if (isIK || name === "hips") {
      timelines.push({ name, type: "position", isIK, times, values: positions });
    }
  }
  return { timelines, duration: lastFrame(motions) / 30 };
}

function fixPositions(tls, vrmOffset = {}) {
  const center = offsetTimeline("center", vrmOffset.hipsOffset);
  const centerMerged = mergeTimelines(tls, "センター", center);
  const hips = mergeTimelines(tls, "全ての親", centerMerged, "下半身");
  tls.set("上半身", localizeTimeline(hips, mergeTimelines(tls, "全ての親", centerMerged, "下半身")));
  tls.set("下半身", hips);
  const lFoot = offsetTimeline("leftFootIK", vrmOffset.leftFootOffset);
  const rFoot = offsetTimeline("rightFootIK", vrmOffset.rightFootOffset);
  if (tls.has("左つま先ＩＫ"))
    tls.set("左つま先ＩＫ", mergeTimelines(tls, "全ての親", lFoot, "左足ＩＫ", offsetTimeline("leftToeIK", vrmOffset.leftToeOffset), "左つま先ＩＫ"));
  if (tls.has("右つま先ＩＫ"))
    tls.set("右つま先ＩＫ", mergeTimelines(tls, "全ての親", rFoot, "右足ＩＫ", offsetTimeline("rightToeIK", vrmOffset.rightToeOffset), "右つま先ＩＫ"));
  if (tls.has("左足ＩＫ"))
    tls.set("左足ＩＫ", mergeTimelines(tls, "全ての親", lFoot, "左足ＩＫ"));
  if (tls.has("右足ＩＫ"))
    tls.set("右足ＩＫ", mergeTimelines(tls, "全ての親", rFoot, "右足ＩＫ"));
  tls.delete("センター");
  tls.delete("全ての親");
  for (const tl of tls.values()) {
    for (const f of tl) {
      f.position.x *= -1;
      f.rotation.x *= -1;
      f.rotation.w *= -1;
      switch (f.boneName) {
        case "左腕": f.rotation.multiply(Z_30_CW); break;
        case "右腕": f.rotation.multiply(Z_30_CCW); break;
        case "左ひじ": case "左手首": case "左親指０": case "左親指１": case "左親指２":
        case "左人指１": case "左人指２": case "左人指３":
        case "左中指１": case "左中指２": case "左中指３":
        case "左薬指１": case "左薬指２": case "左薬指３":
        case "左小指１": case "左小指２": case "左小指３":
          f.rotation.premultiply(Z_30_CCW).multiply(Z_30_CW); break;
        case "右ひじ": case "右手首": case "右親指０": case "右親指１": case "右親指２":
        case "右人指１": case "右人指２": case "右人指３":
        case "右中指１": case "右中指２": case "右中指３":
        case "右薬指１": case "右薬指２": case "右薬指３":
        case "右小指１": case "右小指２": case "右小指３":
          f.rotation.premultiply(Z_30_CW).multiply(Z_30_CCW); break;
      }
      f.position.multiplyScalar(0.1);
    }
  }
}

function offsetTimeline(boneName, rawPos) {
  const init = { leftFootIK: { x: 1, y: 1, z: 0, s: 10, dx: true }, rightFootIK: { x: -1, y: 1, z: 0, s: 10, dx: true }, center: { x: 0, y: 1, z: 0, s: 10 } }[boneName];
  if (!init) return [{ boneName: boneName + "Offset", frameNum: 0, position: new Vector3(), rotation: new Quaternion() }];
  return [{
    boneName: boneName + "Offset", frameNum: 0,
    position: new Vector3(init.x, init.y, init.z).multiplyScalar(init.s ?? 1),
    rotation: new Quaternion(),
  }];
}

function mergeTimelines(tlsMap, ...keys) {
  const tls = keys.map((k) => Array.isArray(k) ? k : tlsMap.get(k)).filter(Boolean);
  const last = keys[keys.length - 1];
  const boneName = typeof last === "string" ? last : (tls[tls.length - 1][0]?.boneName ?? "");
  const results = [];
  for (const tl of tls) {
    for (const f of tl) {
      if (f.frameNum < results.length && results[f.frameNum] != null) continue;
      const pos = new Vector3(), quat = new Quaternion();
      for (const otl of tls) {
        if (!otl.length) continue;
        const f2 = otl[0].boneName === f.boneName ? f : lerpKeyframe(otl, f.frameNum);
        pos.add(tempV3.copy(f2.position).applyQuaternion(quat));
        quat.multiply(f2.rotation);
      }
      results[f.frameNum] = { boneName, frameNum: f.frameNum, position: pos, rotation: quat };
    }
  }
  return results.filter(Boolean);
}

function localizeTimeline(parent, child) {
  const boneName = child[0].boneName;
  const results = [];
  let isChild = false;
  for (const tl of [parent, child]) {
    for (const f of tl) {
      if (f.frameNum < results.length && results[f.frameNum] != null) continue;
      const fp = isChild ? lerpKeyframe(parent, f.frameNum) : f;
      const fc = isChild ? f : lerpKeyframe(child, f.frameNum);
      results[f.frameNum] = {
        boneName, frameNum: f.frameNum,
        position: (fc.isNew ? fc.position : fc.position.clone()).sub(fp.position),
        rotation: (fc.isNew ? fc.rotation : fc.rotation.clone()).multiply(tempQ.copy(fp.rotation).invert()),
      };
    }
    isChild = true;
  }
  return results.filter(Boolean);
}

function lerpKeyframe(tl, frameNum) {
  if (!tl) return { boneName: "", frameNum, position: new Vector3(), rotation: new Quaternion(), isNew: true };
  const nextIndex = tl.findIndex((k) => frameNum < k.frameNum);
  if (nextIndex === 0) return tl[0];
  if (nextIndex === -1) return tl[tl.length - 1];
  const prev = tl[nextIndex - 1], next = tl[nextIndex];
  const v = (frameNum - prev.frameNum) / (next.frameNum - prev.frameNum);
  return {
    boneName: tl[0].boneName, frameNum,
    position: prev.position.clone().lerp(next.position, v),
    rotation: prev.rotation.clone().slerp(next.rotation, v),
    isNew: true,
  };
}

function lastFrame(arr) {
  return arr.length ? arr[arr.length - 1].frameNum : 0;
}
