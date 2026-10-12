# 修复复核记录（2026-10-12）

针对 master 工作区的七项修改，结论为 **3 项针对性验证通过，4 项部分完成**。复核保持只读，未操作真实服务或设备。

| 项目 | 判定 | 证据与剩余问题 |
|---|---|---|
| 1. 进程误匹配 | 部分完成，P1 风险仍在 | 无关目录的同名脚本及 gedit 已排除；Python 普通参数中包含本仓库脚本路径仍会误匹配。atvvoice 路径字符串前缀还接受相近名称的兄弟目录。 |
| 2. 启动恢复和就绪 | 部分完成 | 仅剩浮窗时会启动核心后台。但只检查 current.json 存在，损坏 JSON、不存在的 PID 仍报告成功；未就绪核心进程也会短路。 |
| 3. 停止确认 | 部分完成 | SIGINT/TERM/KILL 后复查生效；模拟残留进程时返回 1、保留元数据。扫描不覆盖 ydotoold/pw-record，强杀父进程后可能遗漏子进程。 |
| 4. 蓝牙占用分类 | 针对性通过 | 实际 bluer 0.17.4 类型中，ConnectionAttemptFailed/NotSupported 不判占用，NotPermitted/InProgress 判占用；NotConnected 文本不判占用。 |
| 5. EOF 和关闭通道 | 原场景通过，边界未完成 | 全部 BLE EOF 返回，可选通道关闭不再空转。RX 单独 EOF 仍等待；CTL EOF 遇背压会遗漏已就绪、未解码的 RX 尾帧。 |
| 6. 动画重启 | 针对性通过 | 每 150 ms 提交相同状态，1.5 秒内启动、完成各 1 次；宽度 430，animating=False。 |
| 7. AUDIO_START 超时 | 针对性通过 | 空闲 6 秒后启动，4.9 秒仍 Streaming，5.1 秒才超时关闭；当前 frame-timeout=0 继续禁用帧超时。 |

项目 1～3 定位于 `toggle_dictation.sh`；4 在接收器 `ble.rs`、`main.rs`；5、7 在 `atvv.rs`；6 在 `floating_preview.py`。

## 验证边界

- 现有 Python 测试 31/31、Qt 离屏测试 4/4 通过，Shell 语法和 Python 语法解析通过。
- Rust harness 直接引用当前会话源码，使用 mock BLE/Protocol；错误分类使用实际 bluer 类型。它验证控制逻辑，不验证真实无线时序或 PipeWire。
- 当次完整 Cargo 测试被缺少 `libpipewire-0.3.pc` 阻断。后续是否重建成功，以新日志和 `artifacts/runtime-rebuild/result.json` 为准。
- 旧音质与输入验收不移用于当前修复。正常 AUDIO_STOP 已有 ready RX 排空；CTL EOF 路径需要单独修复和验证。

本次提交保留当前实现和限制。后续优先补齐入口鉴定、真实就绪与本实例子进程回收，再处理 RX EOF 与 CTL EOF 尾帧。
