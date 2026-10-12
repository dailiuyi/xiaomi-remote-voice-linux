# 小米蓝牙语音遥控器 2 Pro (RC003) 按键映射与 Vibe Coding 手册

本项目通过开源工具 **`input-remapper`** 实现了针对 Linux (Ubuntu 26.04 Wayland / KDE Plasma) 环境下的遥控器硬件级精准映射，与本项目的原生 BLE ATVV 语音转写通道形成无缝互补闭环。

---

## 一、全键位映射总览表 (13 键全覆盖)

| 区域 | 物理按键 | 底层硬件键码 | 映射输出 | 针对场景与功能 |
| :--- | :--- | :--- | :--- | :--- |
| **顶部功能区** | **电源键 (Power)** | `KEY_POWER (116)` | `Ctrl + C` | **任务急停 / 打断**：单手直接中断 Agent 运行或死循环命令 |
| | **TV 专属快捷键** | `KEY_GRAVE (41)` | `Ctrl + L` | **焦点直达**：无论光标在哪，一键瞬间跳转至 AI 对话框 |
| | **语音键** | *BLE GATT ATVV* | *(原生语音链路)* | **自然语言口述**：按住口述 Prompt，松开自动转写并粘贴 |
| **中央导航环** | **方向 上 (UP)** | `KEY_UP (103)` | • 短按：`Up`<br>• 长按(>220ms)：`wheel(up, 4)` | • 输入框内单点：**光标向上微调一行**<br>• 按住不放：**4倍速向上平滑滚动会话历史与代码** |
| | **方向 下 (DOWN)** | `KEY_DOWN (108)` | • 短按：`Down`<br>• 长按(>220ms)：`wheel(down, 4)` | • 输入框内单点：**光标向下微调一行**<br>• 按住不放：**4倍速向下平滑滚动会话历史与代码** |
| | **方向 左 (LEFT)** | `KEY_LEFT (105)` | `Left` | **逐字向左微调光标** |
| | **方向 右 (RIGHT)** | `KEY_RIGHT (106)` | `Right` | **逐字向右微调光标** |
| | **中央确认 (OK)** | `KEY_ENTER (28)` | `Enter` | **发送提示词 / 终端批准执行** |
| **中控核心区** | **返回键 (Back)** | `KEY_BACK (158)` | `BackSpace` (退格连删) | • **单按删除光标前 1 个字**<br>• **按住不放平滑连续向左删字**（修改 Prompt 极快） |
| | **主页键 (Home)** | `KEY_HOME (102)` | `Alt + Tab` | **客户端跳转**：单手一键在 CodeX、Web 浏览器与终端间秒切 |
| | **菜单键 (Menu)** | `KEY_COMPOSE (127)` | `Ctrl + K` | **内联修改 (Inline Edit)** / 呼出命令菜单 |
| **底部音量区** | **音量+ (VOL+)** | `KEY_VOLUMEUP (115)` | `Ctrl + R` | **页面刷新**：单手一键刷新 Web 应用/查看构建效果 |
| | **音量- (VOL-)** | `KEY_VOLUMEDOWN (114)` | `Ctrl + Z` | **极速撤销 (Undo)**：生成不满意或误输入时单手秒撤回 |

---

## 二、架构原理与服务管理

1. **设备隔离机制**：
   - 规则文件仅绑定设备名 `小米蓝牙语音遥控器`。
   - 笔记本自带键盘、罗技外接鼠标及其它外设不受任何影响。
2. **配置文件路径**：
   - 预设文件：`~/.config/input-remapper/presets/小米蓝牙语音遥控器/vibe_coding.json`
   - 全局配置：`~/.config/input-remapper/config.json`
3. **服务状态与启动**：
   - 后台守护进程：`input-remapper-daemon.service`
   - 查看运行状态：
     ```bash
     systemctl status input-remapper-daemon.service
     ```
   - 重新注入预设（如修改配置后）：
     ```bash
     pkexec input-remapper-control --command stop-all && pkexec input-remapper-control --command start --device "小米蓝牙语音遥控器" --preset "vibe_coding" --config-dir "$HOME/.config/input-remapper"
     ```
