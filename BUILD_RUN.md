# 本地构建与运行

命令在仓库根目录执行。Git 只保存源码，`artifacts/` 中的模型、虚拟环境和构建资源需要另行恢复。以下流程不安装系统包，不设置开机自启。

## 构建接收器

`build_receiver.sh` 设置项目内的 Rust/Cargo 目录、输出和 pkg-config sysroot。可用项目内链接复用现有 `/home/elma/.rustup`、`/home/elma/.cargo`；不要覆盖已有独立目录。

在 `artifacts/receiver-build/sysroot` 中解压与本机匹配的 libpipewire-0.3-dev、libspa-0.2-dev、libpipewire-0.3-0t64、libdbus-1-dev、libsystemd-dev、libsystemd0、libc6-dev、libc-dev-bin。Ubuntu 26.04 的 dbus-1 pkg-config 还依赖 libsystemd。需要已有 GCC、pkg-config、libclang。局部解压不改变系统安装状态，软件包和日志保存在 `artifacts/receiver-build/`。

```bash
bash build_receiver.sh test --locked
bash build_receiver.sh build --locked
sha256sum artifacts/receiver-build/target/debug/atvvoice
artifacts/receiver-build/target/debug/atvvoice --help
```

`--help` 只验证可执行文件加载，不能证明蓝牙或音频可用。完整构建和测试必须实际执行；模块 harness 通过不得当作全项目通过。

## 恢复 Qwen 环境

- Python 3.12 虚拟环境：`artifacts/asr-comparison/runtime/venv`。
- 固定核心版本：torch/torchaudio 2.9.1、torchvision 0.24.1、qwen-asr 0.0.6、transformers 4.57.6、faster-whisper 1.2.1、dbus-next 0.2.3、opencc-python-reimplemented 0.1.7。
- 历史版本记录：`artifacts/streaming-runtime/requirements.lock.txt`，是 pip freeze，可能含本机路径或无法重新取得的包，不能不经核对直接整份安装。
- `download_asr_comparison.py` 固定 Qwen 官方 revision 并校验大小/SHA-256。原脚本准备 0.6B、1.7B 两套模型，当前仅需 1.7B，可按同一 manifest 只恢复 1.7B；不得替换版本。
- ydotool 工具：`artifacts/dictation-runtime/tools/usr/bin`；系统 PyQt6 用于浮窗。恢复后检查依赖和 CUDA，保留新的 freeze 与下载哈希。

模型加载阶段使用本地目录和离线模式。运行环境和模型均被 Git 忽略，不随源码提交。

## 使用新构建运行

先查现有 unit、进程、D-Bus 名称和 ATVVoice 音源，健康实例不重复启动。同一遥控器不能由新旧接收器同时控制。

```bash
bash start_streaming_dictation.sh \
  --receiver "$PWD/artifacts/receiver-build/target/debug/atvvoice"
```

后台运行可用用户级临时 unit，工作目录指向本仓库、退出信号为 SIGINT；不 enable 自启。直接使用上述入口等待真实 READY，不依赖开关脚本的文件存在判断。

检查日志 MODEL_READY、PCM_READY、READY；核对 current.json 的进程身份和 preview_url 下 status 的 HTTP 200/ready 状态；确认唯一 `atvvoice-rc003dictation` 音源、录音进程存活，系统默认输入输出不变。用户再点击测试输入框、按住说话并松开，核对预览、单次输入及没有 Enter。

停止优先请求已确认实例正常退出，再检查 receiver、pw-record、ydotoold 和浮窗。开关脚本的已知缺口见 [REVIEW.md](REVIEW.md)。

构建、测试和运行日志，以及源码 commit、二进制 SHA-256、PID 和健康检查，保存在 `artifacts/runtime-rebuild/`。启动、健康检查和人工体验分别报告。
