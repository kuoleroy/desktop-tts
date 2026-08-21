// 音频播放模块（WebAudio 口型同步 + 分块朗读队列）
import { getVrm } from "./app-model.js";

let audioCtx = null;
let analyser = null;
let audioSrc = null;
let mouthLevel = 0;

// 播放队列
let playQueue = [];
let playingId = 0;
let queuePaused = false;
let totalBlocks = 0;
let currentBlock = 0;

// 进度定时器
let progressTimer = null;

const freqData = new Uint8Array(128);

export function getMouthLevel() { return mouthLevel; }
export function getAudioCtx() { return audioCtx; }
export function getAnalyser() { return analyser; }

export function updateMouth() {
  if (!analyser) return;
  analyser.getByteFrequencyData(freqData);
  let sum = 0;
  for (let i = 0; i < 128; i++) sum += freqData[i];
  const level = sum / 128 / 255;
  mouthLevel = mouthLevel * 0.6 + level * 0.4;
  const open = Math.min(1, mouthLevel * 4);
  getVrm()?.expressionManager?.setValue("aa", open);
}

function broadcastPlayState(state) {
  window.__TAURI__?.event?.emit("play-state", state);
}

function broadcastProgress(frac, sec) {
  window.__TAURI__?.event?.emit("read-progress", { frac, sec });
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

function pauseAudio() {
  queuePaused = true;
  if (!audioCtx || !audioSrc) {
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

function playSequence(url, onDone, blockIdx) {
  stopAudio(true);
  audioCtx = audioCtx || new AudioContext();
  if (!audioCtx) {
    onDone();
    return;
  }
  (async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        window.setStatus("播放失败: " + res.statusText);
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
        if (getVrm()) getVrm().expressionManager?.setValue("aa", 0);
        audioSrc = null;
        onDone();
      };
      audioSrc.onerror = () => { audioSrc = null; onDone(); };
      audioSrc.start();
      startProgressTimer(blockIdx);
      broadcastPlayState("playing");
    } catch (e) {
      window.setStatus("播放失败: " + e.message);
      onDone();
    }
  })();
}

function playQueueItem(idx) {
  if (queuePaused) return;
  if (!playQueue[idx]) {
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
    if (myId !== playingId) return;
    playQueueItem(idx + 1);
  };
  playSequence(url, onDone, idx);
}

export function startQueue(paths) {
  stopQueue();
  queuePaused = false;
  playQueue = paths.slice();
  totalBlocks = playQueue.length;
  currentBlock = 0;
  broadcastProgress(0, 0);
  playQueueItem(0);
}

export async function playAudioFrom(blobUrl) {
  stopAudio();
  audioCtx = audioCtx || new AudioContext();
  if (!audioCtx) {
    window.setStatus("AudioContext failed to initialize");
    return;
  }
  try {
    const res = await fetch(blobUrl);
    if (!res.ok) {
      window.setStatus("Failed to fetch audio: " + res.statusText);
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
      if (getVrm()) getVrm().expressionManager?.setValue("aa", 0);
      audioSrc = null;
      broadcastPlayState("idle");
    };
    audioSrc.onerror = (e) => {
      window.setStatus("Audio playback error: " + e.message);
      broadcastPlayState("idle");
    };
    audioSrc.start();
    broadcastPlayState("playing");
  } catch (e) {
    window.setStatus("Audio playback failed: " + e.message);
    broadcastPlayState("idle");
  }
}
window.playAudioFrom = playAudioFrom;

export { stopAudio, pauseAudio, resumeAudio, stopQueue, startProgressTimer, stopProgressTimer };