# Desktop TTS 桌面朗读助手 + 3D 桌宠

一个 Windows 桌面应用：带有可拖动 3D 桌宠 + 全局任意软件选中文本自动朗读。在任意软件中选中文字（双击 / Ctrl+A / 拖选），自动弹出朗读面板，选择音色与语速后即可朗读，也可一键导出 MP3。

- 基于 **Tauri 2.x** 构建，Rust + HTML/CSS/JS 前端，体积轻巧（约 50MB）
- 包含一个可拖动、透明背景的 3D 桌宠（支持 VRM 模型），放在桌面角落不挡鼠标
- **全局文字抓取**：通过 Python + UIA + 模拟复制 + OCR 三级兜底，几乎能抓取任意软件中选中的文字
- 音色由 **edge-tts**（微软 Edge 在线朗读接口）提供，免费、无需 Key、秒级出音频
- 本地 Windows SAPI 语音作为断网兜底，Windows 11 自带高质量神经语音

## 功能特性

- **三种触发抓取**：双击选中文字、拖选松开、Ctrl+A 全选后自动触发
- **三级兜底抓取**：UIA 直读选区 → 模拟 Ctrl+C 读剪贴板（完整还原）→ OCR 截图识别（权限隔离兜底）
- **悬浮框快捷操作**：抓取后弹出悬浮框显示当前音色，点击「设置」打开完整面板
- **朗读面板**：6 种在线真人神经音色（edge）+ 2 种 Windows 本地语音（离线），语速 0.5~2.0、语调 ±15Hz
- **断网自动兜底**：在线音色失败自动切换本机语音，无需手动操作
- **导出 MP3**：当前文本按所选音色/语速/语调合成并导出为 MP3 文件
- **磁盘缓存**：相同文本+参数只合成一次，重复朗读毫秒级返回；可设缓存上限自动清理
- **系统托盘**：可最小化到任务栏（托盘图标）
- **全局快捷键**：
  - `Ctrl+Shift+T`：切换桌宠「观赏/交互」模式
  - `Ctrl+Shift+X`：切换主窗口「鼠标穿透」（穿透不挡鼠标，可点击下方窗口）
- **配置记忆**：音色/语速/语调自动保存，重启沿用

## 环境要求

- Windows 10/11（必须，依赖 UIA 和 Tauri Windows 窗口特性）
- Rust 1.75+（编译 Tauri 应用）
- Python 3.10+（已测试 3.10 / 3.13）
- 朗读需联网（edge-tts 在线接口，断网自动兜底到本地语音）

## 快速开始

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

注意：`edge-tts` 需从官方 PyPI 安装（清华源可能暂未同步 7.2.8）：
```bash
pip install -i https://pypi.org/simple/ edge-tts==7.2.8
```

### 2. 编译运行

```bash
# 开发模式（启动 dev 服务器 + Tauri 窗口）
cargo tauri dev

# 编译发行版
cargo tauri build
```

## 项目结构

```
desktop-tts/
├── assets/web/          # 前端 HTML/CSS/JS（Three.js + VRM 桌宠 + 面板/悬浮框）
│   ├── app.js           # 3D 桌宠主逻辑
│   ├── panel.js         # 设置面板
│   ├── floater.js       # 悬浮框
│   └── crop.js          # 全屏框选 OCR
├── sidecar/             # Python 侧车进程（独立进程，隔离崩溃）
│   ├── tts_sidecar.py   # TTS 合成服务（edge-tts + SAPI 兜底）
│   └── tts_grabber.py   # 全局选区抓取（UIA + 模拟复制 + OCR 三级兜底）
├── src-tauri/           # Rust 主应用（Tauri 窗口 + 进程管理 + IPC）
│   └── src/main.rs      # 主入口，窗口管理、进程看门狗、命令分发
├── models/              # VRM 3D 模型存放目录
├── README.md            # 本文档
└── requirements.txt     # Python 依赖列表
```

### 架构设计

- **Rust（Tauri）**：创建窗口、管理生命周期、监控子进程、处理全局快捷键、IPC 通信
- **Python**：两个独立子进程：
  - `tts_sidecar.py`: TTS 文本转语音服务，edge-tts 在线合成，缓存管理
  - `tts_grabber.py`: 全局文本抓取，pynput 钩子监听双击/拖选/Ctrl+A，三级兜底读取选区
- **看门狗机制**：Rust 侧监控两个 Python 进程，崩溃后 1.5 秒内自动重启，保证服务可用性
- **三级抓取链路**：
  1. **UIA 直读**：遍历前台窗口 UIA 树，读取 TextPattern/SelectionPattern 选区
  2. **模拟 Ctrl+C**：UIA 读不到但确有选区时，模拟 Ctrl+C 复制到剪贴板读取，读完完整还原剪贴板（接近无损）
  3. **OCR 截图识别**：UIPI 权限隔离（管理员应用 vs 普通进程）导致模拟复制被拦截时，截取选区用 RapidOCR 识别文字

## 使用说明

### 触发朗读

| 操作 | 说明 |
| --- | --- |
| 双击 | 双击选中一个词/句后触发 |
| 拖选 | 按住左键拖选文字，松开后触发 |
| Ctrl+A | 全选当前文档后触发 |

触发后悬浮框自动弹出到选区下方，显示当前音色；点击「设置」打开完整面板。

### 面板按钮

`朗读 / 暂停 / 框选识别 / 导出MP3 / 停止 / 收起`

- **框选识别**：全屏进入框选模式，拖拽鼠标框选任意区域文字，OCR 识别后朗读
- **导出MP3**：按当前音色/语速/语调合成当前文本并保存为 MP3 到下载文件夹
- **收起**：面板收回，悬浮框隐藏，回到桌宠观赏模式

### 右键菜单（在桌宠上右键）

- **显示面板**：打开设置面板
- **恢复默认窗口大小**：主窗口恢复到 240×300 默认尺寸
- **最小化到托盘**：隐藏所有窗口，仅保留托盘图标
- **退出**：退出程序，干净关闭所有子进程

## 配置文件

### `voices.json`（音色配置）

音色、语速、语调列表集中在此文件，修改后重启生效：

```json
{
  "voices": {
    "xiaoXiao": {
      "name": "晓晓·女",
      "desc": "自然女声（edge 在线）",
      "edge": "zh-CN-XiaoxiaoNeural"
    }
  },
  "sapi_voices": {
    "xiaoXiaoLocal": {
      "name": "晓晓·本地",
      "desc": "Windows 自带神经语音（离线）",
      "sapi": "local:Microsoft Xiaoxiao (Natural)"
    }
  },
  "speed_list": ["0.5", "0.7", "1.0", "2.0"],
  "pitch_list": ["-15Hz", "-5Hz", "+0Hz", "+15Hz"],
  "edge_max_single": 2000
}
```

添加新音色：往 `voices` 里加一项即可，界面自动出现。

#### 本地语音（离线兜底）

`sapi_voices` 下的本地音色走 Windows 自带 `System.Speech`：

- **Windows 11**：自带高质量神经语音（Microsoft Xiaoxiao / Yunxi Natural），音质接近在线音色
- **Windows 10**：一般只有传统语音，音质一般，但**完全离线**
- 在线音色合成失败（断网/接口变更）时，自动用本机语音兜底，无需手动切换

## 日志

- `sidecar/sidecar_stderr.log`: TTS 合成进程 stderr 日志
- `sidecar/grabber_stderr.log`: 文本抓取进程 stderr 日志（用于诊断抓取失败）
- `diag.log`: Rust 主程序 panic 日志和异步日志落盘

## 常见问题

**Q: 选中文字没有反应？**
- 确认抓取进程已启动（看 `grabber_stderr.log` 是否有启动日志）
- 查看日志 `sidecar/grabber_stderr.log` 定位具体原因

**Q: 在终端（Windows Terminal/cmd.exe）里选文字会触发 Ctrl+C 把程序杀掉？**
- 已修复：代码中检测前台是终端类窗口时，会跳过 Ctrl+C 注入

**Q: 为什么用两个独立 Python 进程？**
- 抓取进程和合成进程隔离：一方崩溃不影响另一方，看门狗单独重启崩溃方，提高可用性
- 抓取进程需要 STA 线程套间（UIA 事件要求），合成进程不需要，分离更稳定

**Q: 端口 8848 被占用？**
- 服务只在本机回环监听，若被其他程序占用，先退出占用程序或重启本程序。

## 许可证

仅供个人学习使用。edge-tts 为微软公开接口，请遵守其服务条款。
