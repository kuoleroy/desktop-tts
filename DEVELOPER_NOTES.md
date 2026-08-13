# 开发者笔记 / AI 上下文（Desktop-TTS 桌宠朗读器）

> 目的：让后来的开发者或 AI 工具快速理解本项目的架构、已修复的问题与易踩的坑，
> 避免重复踩坑或破坏已跑通的链路。请先读本文件再动代码。

---

## 1. 项目是什么

一个 **Tauri 2 桌面桌宠**应用：
- 一个悬浮在桌面的 3D 桌宠窗口（Three.js + VRM 模型渲染）。
- 一个**交互面板**（朗读控制台）：输入/抓取文字 → 在线 TTS 朗读、切音色/语速/语调、导出 MP3。
- 两种模式：**观赏模式（watch）** 只显示 3D 桌宠；**交互模式（interact）** 显示面板。

技术栈：
- **Rust + Tauri 2**：窗口、事件、命令、sidecar 进程管理。
- **Python sidecar**（`sidecar/tts_sidecar.py`）：NDJSON 行协议，负责 TTS 合成。
- **edge-tts**（在线，可切音色）为主，**SAPI（win32com）** 为离线兜底。
- 前端 **Three.js + three-vrm** 渲染 VRM 模型。

---

## 2. 如何运行

依赖：
- `edge-tts` 必须安装（**官方 PyPI 源**，清华镜像没有）：`python -m pip install edge-tts -i https://pypi.org/simple`
- `pywin32`（SAPI 兜底）。

开发模式运行（**推荐用 dev，不要直接打包**，便于看日志/热更新）：

```bash
cd src-tauri
cargo tauri dev
```

- sidecar 由 Rust 启动，`PYTHON` 环境变量可指定解释器，缺省用 PATH 里的 `python`。
- `diag.log` 生成在项目根目录（`e:\kuoleroy\desktop-tts\diag.log`），是主要排障依据；
  edge-tts 失败时另有 `sidecar/sidecar_edge.log`。

---

## 3. 运行时架构与关键链路

### 3.1 窗口与模式切换
- `tauri.conf.json` 里声明两个窗口：`main`（桌宠）和 `panel`（面板，`visible: true`，**启动即创建**）。
- 模式通过 **`toggle-mode` 事件**广播给两个窗口（`main:watch/interact`、`panel:watch/interact`）。
- 启动时：面板 `panel-ready` 事件 → Rust 把面板定位到主窗口右侧并 `show()`，置为交互态。
- `Ctrl+Shift+T` 或面板"返回观赏"（`panel-closing` 事件）→ 隐藏面板、切回观赏态。
- **面板必须声明在 `tauri.conf.json` 中创建**，不要改成程序化创建（见 4.3）。

### 3.2 TTS 链路
```
面板 JS (panel.js) --invoke--> Rust 命令 --NDJSON--> sidecar python
                                                 --合成 mp3--> 写 tts_cache/
Rust --emit "tts" (文件路径)--> 前端 app.js 用 asset 协议播放
```
- 命令：`read_text` / `stop_read` / `set_voice` / `set_rate` / `set_pitch` / `export_mp3` / `list_models` / `model_dir` / `quit`。
- edge-tts 成功产出 `.mp3`；失败回退 SAPI 产出 `.wav`（**看到 `.wav` 说明 edge-tts 没走通**）。

### 3.3 模型加载
- 模型在根目录 `models/`（当前 `AliciaSolid.vrm`）。
- Rust 动态注册 **asset protocol scope**（`app.asset_protocol_scope().allow_directory(...)`），
  前端用 `convertFileSrc` 构造 `asset.localhost` URL 加载。

---

## 4. 已修复的问题（及根因）—— 改动时务必不要回退

### 4.1 面板不出来 / 第二个 WebView 不初始化
- **根因**：面板此前在 `setup()` 里用代码创建并 `visible(false)`，WebView2 对隐藏窗口懒加载，
  导致面板 WebView 从未初始化（日志 `panel init: inner=None`），怎么 `show()` 都不出现。
- **修复**：面板改为在 `tauri.conf.json` 声明创建（`visible: true`），启动即初始化。
  切换用 `show()/hide()` + `set_position`，不再重建窗口。

### 4.2 模型 403 Forbidden（Tauri asset 协议）
- **根因**：Tauri 2 的 asset protocol **不支持 `$CARGO_MANIFEST_DIR` 变量**，配置里的 scope 无效；
  且路径含 `..` 会被 scope 校验拒绝。
- **修复**：改用运行时 `scope.allow_directory()`，并对模型目录/缓存目录用 `canonicalize()` 消除 `..`。
- **注意**：凡新增供 asset 协议访问的目录，都要走 `canonicalize()` + 动态 scope，别手写带 `..` 的路径。

### 4.3 朗读测试闪退/无声音
- 音频路径含 `..` 导致 403 → `cache_dir()` canonicalize 修复。
- WebView2 自动播放被拦截 → `tauri.conf.json` 加 `--autoplay-policy=no-user-gesture-required`。
- 前端收到 `tts` 事件的路径含反斜杠 → 需 `replace(/\\/g, "/")` + `convertFileSrc`。

### 4.4 音色切换无效（重点，本次刚修）
- 三个叠加根因，都在 `sidecar/tts_sidecar.py`：
  1. **edge-tts 未安装** → `import edge_tts` 失败，一直回退 SAPI（.wav、固定系统音色）。
  2. **异常捕获写错名**：`except (asyncio.CancelledError, concurrent_futures.CancelledError)`
     中的 `concurrent_futures` 不存在（应为 `concurrent.futures`），导致每次合成一旦触发异常匹配
     就抛 `NameError`，被当成失败回退 SAPI。
  3. **stdin 编码错位**：中文 Windows 下 `sys.stdin` 默认按 GBK 码页 + `surrogateescape` 读取，
     Rust 发来的 UTF-8 中文被读成乱码和孤立代理（`\udcxx`），edge-tts 编码时 `UnicodeEncodeError`。
     → 已在 `main()` 里强制 `sys.stdin/sys.stdout.reconfigure(encoding="utf-8", errors="strict")`。
- **判定**：`diag.log` 里 `"mp3": "...mp3"` = edge-tts 走通；`"...wav"` = 走了 SAPI 兜底。

---

## 5. 注意事项 / 易踩的坑

1. **不要在 `setup()` 闭包里 `Arc::clone(&app_handle)` 报 Copy 错误**——用闭包参数 `app.clone()`。
2. **sidecar 进程要在 `quit` 命令里 kill**，否则退出后残留孤儿 python。
3. **WebView2 残留进程**会干扰窗口/面板创建（闪退后尤其明显）。`diag.log` 显示面板
   `inner=None` 时，先结束所有 `msedgewebview2` 进程再重启。
4. **前端 `window.__TAURI__` 是异步注入的**：顶层脚本执行时可能还没有。访问前要等待
   `tauriReady()`；所有 TTS 方法加 `__TAURI__` 运行时守卫，否则浏览器预览会崩。
5. **dev_server.py 必须双栈监听（IPv4+IPv6）**，否则 WebView2 连不上；仅供 dev 预览，
   不是真实运行路径。
6. **日志**：Rust 日志走 `diag.log`，需周期性 flush（`recv_timeout`）否则空闲时不落盘；
   panic hook 会同步写 `diag.log`，崩溃后先看它。
7. **面板/桌宠窗口**：透明、无边框、置顶、不占任务栏（见 `tauri.conf.json`）。

---

## 6. 目录速览

- `src-tauri/src/main.rs`：窗口/事件/命令/sidecar 管理/asset scope/日志。
- `src-tauri/tauri.conf.json`：窗口声明、autoplay 参数。
- `assets/web/index.html + app.js`：桌宠 3D 渲染与音频播放。
- `assets/web/panel.html + panel.js`：交互面板逻辑。
- `sidecar/tts_sidecar.py`：TTS 合成（edge-tts + SAPI 兜底）、可打断后台任务。
- `dev_server.py`：dev 静态服务（双栈）。
- `models/`：VRM 模型；`tts_cache/`：合成音频缓存（运行时生成）。
- `diag.log` / `sidecar/sidecar_edge.log`：排障日志。
