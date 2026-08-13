# 修改记录 / 变更说明（Desktop-TTS 桌宠朗读器）

> 目的：记录每一次「修改」的原因、做法与注意事项，供后续开发者或 AI 工具理解
> 「为什么要这么改」以及「以后改这里要小心什么」。配合 `DEVELOPER_NOTES.md`（架构）阅读。
>
> 阅读顺序建议：先 `DEVELOPER_NOTES.md` 了解架构，再 `CHANGE_NOTES.md` 了解改动脉络。

---

## 一、本次（最近一轮）改动清单

### 1. 面板按钮交互优化
- **改动文件**：`assets/web/panel.html`、`assets/web/panel.js`、`assets/web/app.js`
- **内容**：
  - 按钮统一朴素样式；「进行中」才变蓝（蓝色 = 激活/占用中）。
  - 新增「暂停 / 开始」按钮（暂停播放、再点恢复）。
  - 「停止」会让所有按钮恢复朴素。
  - 主窗口通过 `play-state` 广播 `playing / paused / idle` 三态，面板据此同步按钮。
- **注意事项**：状态同步走事件而非各自维护，改按钮状态时别单独改某一处，避免不同步。

### 2. 「抓取选中文字」重构为独立进程 + 开关式
- **改动文件**：`sidecar/tts_grabber.py`（新）、`src-tauri/src/main.rs`、`assets/web/panel.js`
- **内容**：
  - 抓取逻辑从 TTS sidecar 拆出为**独立进程** `tts_grabber.py`，与 TTS 进程隔离，互不拖累。
  - 抓取进程配 **watchdog**：崩溃后 Rust 每 1.5s 检查并自动重启。
  - 抓取改成**开关式**：点「抓取」开启（蓝）→ 持续监控选区，可连续抓多段；
    再点一次关闭。Rust 命令 `arm_grab` → `toggle_grab(on: bool)`，内部发 `arm / disarm`。
- **注意事项**：
  - 命令名已从 `arm_grab` 改为 `toggle_grab`，前端 `invoke` 参数为 `{ on: boolean }`。
  - 抓到文字后**保持武装**（不自动解除），直到收到 `disarm`。

### 3. 选区读取：UIA 优先 + 剪贴板兜底
- **改动文件**：`sidecar/tts_grabber.py`
- **内容**：
  - 恢复 **UIA 全树遍历（深度 10）** 作为首选，绝大多数应用（记事本/浏览器/Office）能直接命中选区。
  - **剪贴板仅作最后兜底**：模拟 `Ctrl+C` → 读剪贴板 → 还原原剪贴板。
- **注意事项（重要）**：
  - **不要**再把 UIA 遍历深度砍小（如 4）——那样 UIA 常抓不到，会频繁走剪贴板兜底，
    副作用是反复模拟 `Ctrl+C` 覆盖用户剪贴板（用户已明确反感这一点）。
  - 剪贴板兜底必须「先读旧值 → 复制 → 读新值 → 还原旧值」，否则会吃掉用户已复制的内容。

---

## 二、修改时易踩的坑（给 AI / 开发者）

1. **sidecar 进程必须 kill**：`quit` 命令里要结束 `tts_sidecar.py` 与 `tts_grabber.py`，否则退出后残留孤儿 python。
2. **WebView2 残留进程**会干扰窗口/面板创建；面板 `inner=None` 时先清 `msedgewebview2` 再重启。
3. **stdin/stdout 强制 UTF-8**：中文 Windows 默认 GBK，`main()` 里必须
   `sys.stdin/sys.stdout.reconfigure(encoding="utf-8", errors="strict")`，否则中文乱码。
4. **异常捕获模块名**：`concurrent.futures`（带点），写成 `concurrent_futures` 会 NameError。
5. **Tauri asset 协议**：路径要 `canonicalize()` 消除 `..`，并动态 `scope.allow_directory()`；
   别手写带 `..` 的路径（会 403）。
6. **自动播放被拦截**：`tauri.conf.json` 需 `--autoplay-policy=no-user-gesture-required`。
7. **前端事件路径**：收到的音频路径反斜杠要 `replace(/\\/g, "/")` + `convertFileSrc`。
8. **`window.__TAURI__` 异步注入**：所有 TTS 方法加 `__TAURI__` 运行时守卫，浏览器预览才不会崩。
9. **dev_server.py 双栈监听（IPv4+IPv6）**：否则 WebView2 连不上（仅 dev 预览用）。
10. **面板窗口声明在 `tauri.conf.json`**（`visible: true`），不要程序化创建再隐藏——WebView2 对隐藏窗口不初始化。

---

## 三、后续可开发的项目 / 功能方向

> 按投入产出比排序，均为本应用能力边界内的自然延伸。

1. **TTS 音色/语速/语调预览与收藏**：面板内一键试听不同音色，保存「常用配置」，
   重启后自动加载（当前 `state` 是进程内存态，重启丢失）。
2. **朗读历史与收藏夹**：抓取/朗读过的文字存本地，侧边列表快速回听、导出、整理。
3. **多语言 / 多引擎切换 UI**：edge-tts 支持多语言音色，面板加语言分组下拉。
4. **鼠标悬停即朗读**（可选增强）：抓取开启时，悬停选中文字自动朗读（需做去抖，防误读）。
5. **打包与自动更新**：`tauri build` 产出安装包；接 `tauri-plugin-updater` 做静默升级。
6. **开机自启 + 托盘菜单**：常驻托盘，右键快速打开面板/退出。
7. **配置文件化**：音色、语速、面板位置、窗口尺寸持久化到 `settings.json`，重启不丢。
8. **3D 桌宠扩展**：多套 VRM 模型、换装、表情/口型动画与 TTS 同步。
9. **无障碍/快捷键增强**：全局热键自定义（朗读/暂停/抓取），替代硬编码 `Ctrl+Shift+T`。

---

## 四、待修复的 bug / 已知问题（TODO）

1. **剪贴板兜底频率**：当前只在 UIA 抓不到时才走剪贴板，但若某应用 UIA 始终失效，
   仍可能高频触发 Ctrl+C。后续可改为「检测到鼠标拖动结束再读一次」，而非固定 0.3s 轮询。
2. **多选区覆盖**：开关式抓取连续命中时，最后一次覆盖文本框内容；若用户想保留多段，
   需要「追加」而非「覆盖」选项。
3. **edge-tts 网络失败回退慢**：断网时 edge-tts 会超时后才回退 SAPI，期间 UI 像「卡住」；
   可加超时上限 + 提示。
4. **面板位置在某些高分屏/多屏**：抓取后面板定位到鼠标附近，未做屏幕边界收敛，可能在屏外。
5. **暂停实现**：暂停走 `play-state` 事件 + 音频 `pause()/play()`；若浏览器预览与真实 WebView
   行为不一致，需在两处分别验证。
6. **模型加载失败无显式提示**：模型路径错误时只有日志，用户无感知；建议加载失败弹气泡。

---

## 五、验证清单（改动后必跑）

- `python -m py_compile sidecar/tts_grabber.py sidecar/tts_sidecar.py`（Python 语法）
- `cd src-tauri && cargo check`（Rust 编译）
- 运行 `cargo tauri dev` 后检查：
  - 面板启动即显，按钮样式切换正确（进行中变蓝）。
  - 朗读 / 暂停 / 停止 / 导出 均正常。
  - 点「抓取」→ 在其它软件选文字 → 面板跟随并填充 → 再点「抓取」关闭。
  - 观察 `diag.log` 与 `sidecar/grabber_stderr.log` 确认抓取命中、无报错。
- 退出后确认无残留 `python` / `msedgewebview2` 孤儿进程。
