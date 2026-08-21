// 音色管理模块：内置音色列表 + 自导入 API 分组

const EDGE_VOICES = [
  ["zh-CN-XiaoxiaoNeural", "晓晓（自然女声·在线）"],
  ["zh-CN-YunxiNeural", "云希（阳光男声·在线）"],
  ["zh-CN-YunyangNeural", "云扬（沉稳男声·在线）"],
  ["zh-CN-XiaoyiNeural", "晓伊（活泼女声·在线）"],
  ["zh-CN-YunjianNeural", "云健（运动男声·在线）"],
];
const LOCAL_VOICES = [
  ["local:Microsoft Xiaoxiao (Natural)", "晓晓·本地自然音（离线）"],
  ["local:Microsoft Yunxi (Natural)", "云希·本地自然音（离线）"],
];
// OpenAI 兼容类型无「列举音色」接口时的内置兜底
const OPENAI_DEF_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"];

function providerVoices(p) {
  if (p.voices && p.voices.length) return p.voices;
  if (p.type === "azure") return [];
  return OPENAI_DEF_VOICES;
}

function _opt(v, text, disabled) {
  const o = document.createElement("option");
  o.value = v;
  o.textContent = text;
  if (disabled) o.disabled = true;
  return o;
}

function _safeName(name) { return String(name).replace(/:/g, "·"); }

// 重建音色下拉：先内置，再追加每个自导入 API 的分组
function buildVoiceOptions(current) {
  const sel = $("sel-voice");
  if (!sel) return;
  sel.innerHTML = "";
  const ogOn = document.createElement("optgroup");
  ogOn.label = "微软在线（edge-tts）";
  EDGE_VOICES.forEach(([v, t]) => ogOn.appendChild(_opt(v, t)));
  sel.appendChild(ogOn);
  const ogLo = document.createElement("optgroup");
  ogLo.label = "本地离线（SAPI）";
  LOCAL_VOICES.forEach(([v, t]) => ogLo.appendChild(_opt(v, t)));
  sel.appendChild(ogLo);
  (window.__providerList || []).forEach((p) => {
    const og = document.createElement("optgroup");
    og.label = _safeName(p.name) + (p.type === "azure" ? " · Azure" : " · API");
    const list = providerVoices(p);
    if (list.length) {
      list.forEach((pv) => og.appendChild(_opt("api:" + _safeName(p.name) + ":" + pv, pv)));
    } else {
      og.appendChild(_opt("", "（音色未拉取，请先拉取音色）", true));
    }
    sel.appendChild(og);
  });
  if (current) sel.value = current;
}

// ---- 管理自导入音色 API ----
let __apiEditing = null;

function typeLabel(t) {
  if (t === "azure") return "Azure 语音";
  if (t === "custom") return "通用 HTTP";
  return "OpenAI 兼容";
}

function _mkBtn(text, fn, del) {
  const b = document.createElement("button");
  b.className = "mitem" + (del ? " del" : "");
  b.textContent = text;
  b.addEventListener("click", fn);
  return b;
}

async function openApiMgr() {
  const mgr = $("api-mgr");
  if (!mgr || !window.__TAURI__?.core) return;
  mgr.classList.remove("modal-hidden");
  resetApiForm();
  await refreshApiList();
}

function closeApiMgr() { const mgr = $("api-mgr"); if (mgr) mgr.classList.add("modal-hidden"); }

async function refreshApiList() {
  const data = await TTS.providers();
  window.__providerList = (data && data.providers) || [];
  const listEl = $("api-mgr-list");
  if (!listEl) return;
  listEl.innerHTML = "";
  if (!window.__providerList.length) {
    const d = document.createElement("div");
    d.className = "api-item";
    d.textContent = "（还没有自导入的 API，请在下方添加）";
    listEl.appendChild(d);
    return;
  }
  window.__providerList.forEach((p) => {
    const item = document.createElement("div");
    item.className = "api-item";
    const nm = document.createElement("div");
    nm.className = "nm";
    nm.textContent = p.name + " · " + typeLabel(p.type) + " · " + providerVoices(p).length + " 音色";
    nm.title = JSON.stringify(p);
    item.appendChild(nm);
    item.appendChild(_mkBtn("拉取音色", () => doFetch(p.name), false));
    item.appendChild(_mkBtn("编辑", () => { __apiEditing = p.name; fillApiForm(p); }, false));
    item.appendChild(_mkBtn("删除", () => removeProvider(p.name), true));
    listEl.appendChild(item);
  });
}

async function doFetch(name) {
  showHint("正在拉取 " + name + " 的音色...");
  try {
    const voices = await TTS.fetchProviderVoices(name);
    const p = window.__providerList.find((x) => x.name === name);
    if (p) p.voices = voices;
    await TTS.saveProviders({ providers: window.__providerList });
    buildVoiceOptions($("sel-voice").value);
    await refreshApiList();
    showHint("已拉取 " + voices.length + " 个音色（" + name + "）");
  } catch (e) {
    showHint("拉取失败：" + e.message, true);
  }
}

function syncTypeFields() {
  const t = $("f-type").value;
  const az = $("f-azure"), oa = $("f-openai");
  if (az) az.classList.toggle("fhide", t !== "azure");
  if (oa) oa.classList.toggle("fhide", t === "azure");
}

function resetApiForm() {
  __apiEditing = null;
  ["f-name", "f-region", "f-key", "f-base", "f-key-openai"].forEach((id) => { const el = $(id); if (el) el.value = ""; });
  const m = $("f-model"); if (m) m.value = "tts-1";
  const t = $("f-type"); if (t) t.value = "azure";
  syncTypeFields();
}

function fillApiForm(p) {
  __apiEditing = p.name;
  ["f-region", "f-base", "f-key", "f-key-openai"].forEach((id) => { const el = $(id); if (el) el.value = ""; });
  const n = $("f-name"); if (n) n.value = p.name;
  const t = $("f-type"); if (t) t.value = p.type || "openai";
  ["region", "base", "key"].forEach((k) => { const el = $("f-" + k); if (el && p[k] != null) el.value = p[k]; });
  const m = $("f-model"); if (m) m.value = p.model || "tts-1";
  const ko = $("f-key-openai"); if (ko && p.key) ko.value = p.key;
  syncTypeFields();
}

async function saveApiForm() {
  const name = ($("f-name").value || "").trim();
  if (!name) { showHint("请填写 Provider 名称", true); return; }
  if (/[:"]/.test(name)) { showHint("名称不能包含冒号或引号", true); return; }
  const t = $("f-type").value;
  const p = { name };
  if (t === "azure") {
    p.type = "azure";
    p.region = ($("f-region").value || "").trim();
    p.key = ($("f-key").value || "").trim();
  } else {
    p.type = t;
    p.base = ($("f-base").value || "").trim();
    p.model = ($("f-model").value || "").trim() || "tts-1";
    p.key = ($("f-key-openai").value || "").trim();
  }
  const exist = window.__providerList.find((x) => x.name === name);
  if (exist && exist.voices) p.voices = exist.voices;
  const idx = window.__providerList.findIndex((x) => x.name === name);
  if (idx >= 0) window.__providerList[idx] = p;
  else window.__providerList.push(p);
  try {
    await TTS.saveProviders({ providers: window.__providerList });
    buildVoiceOptions($("sel-voice").value || ("api:" + _safeName(name) + ":" + (providerVoices(p)[0] || "")));
    await refreshApiList();
    resetApiForm();
    showHint("已保存：" + name);
  } catch (e) {
    showHint("保存失败：" + e.message, true);
  }
}

function removeProvider(name) {
  __apiEditing = null;
  window.__providerList = window.__providerList.filter((x) => x.name !== name);
  TTS.saveProviders({ providers: window.__providerList })
    .then(() => {
      buildVoiceOptions($("sel-voice").value);
      refreshApiList();
      showHint("已删除：" + name);
    })
    .catch((e) => showHint("删除失败：" + e.message, true));
}