// 3D 模型加载模块
import * as THREE from "three";
import { GLTFLoader } from "./vendor/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "./vendor/three-vrm.module.min.js";

let vrm = null;
let currentModel = "";

const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));

export function initLoader() {
  // 加载器已在模块初始化时注册 VRM 插件，此函数保持兼容
}

export function getVrm() { return vrm; }
export function getCurrentModel() { return currentModel; }

export async function loadVRM(url, scene, danceLoadFn, greetingFn) {
  const MAX_ATTEMPTS = 3;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
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
      window.__armsDown = false;
      currentModel = url.split("/").pop();
      danceLoadFn();
      greetingFn();
      return;
    } catch (e) {
      window.__errs.push(`loadVRM ${attempt}/${MAX_ATTEMPTS}: ${e.message} @ ${url}`);
      console.error("[loadVRM]", attempt, e);
      if (attempt < MAX_ATTEMPTS) {
        await new Promise((r) => setTimeout(r, 1500));
      } else {
        window.setStatus("加载失败: " + e.message);
      }
    }
  }
}

export async function modelUrl(name) {
  const isTauri = await window.tauriReady();
  if (isTauri && window.__TAURI__?.core?.convertFileSrc) {
    const dir = await window.call("modelDir");
    return window.__TAURI__.core.convertFileSrc(
      dir.replace(/\\/g, "/") + "/" + name
    );
  }
  return "models/" + name;
}

export async function loadModelByIndex(i) {
  const list = await window.call("listModels");
  if (list && list[i]) {
    const name = list[i].split(/[\\/]/).pop();
    loadVRM(await modelUrl(name));
  }
}