# AGENTS.md — Mi遥控

适用于本仓库。接收器另遵守 `receiver/atvvoice/AGENTS.md`；其中上游 NixOS 环境、机器地址和发布流程是背景资料，不能作为本机运行配置或发布授权。

## 工作区与结构

- 主机 `elma-ubuntu-pc`，用户 `elma`，仓库 `/home/elma/文档/ChatGPT/Mi遥控`。先核对分支和已有改动。
- 链路：RC003-MS → BlueZ/ATVV → PipeWire 专属音源 → Python 收音 → 本地 ASR → KDE Klipper/ydotool Ctrl+V。
- 主入口 `start_streaming_dictation.sh` 使用 Qwen 1.7B；`start_dictation.sh` 保留 Whisper medium。两套不能同时控制接收器。
- 环境、模型和证据位于被忽略的 `artifacts/`。源码存在不代表运行资产已恢复，先核对 `BUILD_RUN.md` 和实际目录。

## 操作边界

- 默认中文；区分事实、推测和未验证部分。保留用户改动，不 reset、不覆盖或清理未确认的录音与证据。
- “提交”只授权本地 commit；推送、合并、外发或发布需要明确授权。模型、录音、转写和凭据不得纳入 Git。
- 只读 review 不修补；按用户指令判断是否允许小修。提交当前代码不表示必须修完所有审查问题。
- 启动前检查 unit、进程、D-Bus 名称和 PipeWire 音源，已有健康实例不重复启动。切换构建只处理本实例拥有的进程。
- 不改系统代理、默认音频设备、sudoers 或设备权限。构建库优先局部解压到 `artifacts/receiver-build/sysroot`。
- `toggle_dictation.sh` 仍有进程误匹配和强杀子进程清理缺口。不得依赖它对陌生进程批量终止，先核对实际入口、进程身份和父子关系。
- 语音本地推理，不上传。真实按键、音质和焦点输入由用户验收；测试不得向真实输入框自动发送文字。

## 构建与验证

- Rust 使用 `Cargo.lock` 和 `--locked`。`build_receiver.sh` 指定项目内 Rust/Cargo 路径和 sysroot，先准备环境，见 `BUILD_RUN.md`。
- 接收器功能变更遵守子目录 TDD 要求。模块或 mock harness 通过不代表整个 crate、BlueZ 或 PipeWire 通过。
- Python 测试需要 NumPy；Qt 使用系统 PyQt6，测试设置 `QT_QPA_PLATFORM=offscreen`。
- `test_streaming_dictation` 会写 `RUNTIME/last-transcript.txt`；测试时将 `streaming_dictation.RUNTIME` 指向临时目录，避免覆盖真实转写。
- 文档/脚本变更做对应语法和一致性检查，不扩大为无关重构。已知缺口见 `REVIEW.md`，修复后重跑对应触发用例。

## 运行与证据

- 默认启动引用历史 `atvvoice-tested`。验证新构建必须用 `--receiver` 显式指定路径，记录源码 commit、二进制 SHA-256、命令行和日志。
- READY 应对应模型加载、唯一遥控器音源和实际 PCM。`--status`、元数据存在、进程启动都不足以证明就绪。
- 核对预览 status 接口、专属音源和子进程，不把随机访问路径或口述内容输出到不必要的报告。
- 分开报告已保存、已提交、已编译、已启动、健康检查和设备/人工验收。历史 115 项 Rust 测试或旧二进制试听不能移用于新构建。
- 运行回执和日志保存在 `artifacts/`。停止后确认 receiver、recorder、keyboard 均退出，再报告停止成功。
