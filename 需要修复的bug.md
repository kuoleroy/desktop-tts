# 需要修复的 Bug

## 1. 抓取进程在终端中误发 Ctrl+C

**现象**：在 Windows Terminal 中（运行 `cargo tauri dev` 时），双击/拖选文字会触发抓取进程向终端注入 Ctrl+C，导致 dev 进程被 SIGINT 中断。

**触发链路**：
1. pynput 全局钩子监听到双击/拖选/Ctrl+A → 调用 `_selread_job`
2. `_uia_scan_selection` 扫描到终端有选区矩形（终端光标/选中文本也算）
3. 因 `rect` 不为空，进入 `_auto_copy_fallback`→`_send_ctrl_c`
4. Ctrl+C 注入到 Windows Terminal → SIGINT 杀掉 `cargo tauri dev` 进程

**根因**：
- `_is_console_foreground()` 只检测了旧版 cmd.exe 的 `ConsoleWindowClass`，未检测 Windows Terminal 的 `CASCADIA_HOSTING_WINDOW_CLASS`
- 导致 `_send_ctrl_c` 和 `_auto_copy_fallback` 中的终端防护检查失效

**待修复方案**：
- 在 `_is_console_foreground()` 中增加 `CASCADIA_HOSTING_WINDOW_CLASS` 检测
- 或在 `_selread_job` 中，当 `rect` 不为空但从 UIA 读不到文字时，先检查前台是否为终端再决定是否走 Ctrl+C 兜底

**影响范围**：`sidecar/tts_grabber.py`