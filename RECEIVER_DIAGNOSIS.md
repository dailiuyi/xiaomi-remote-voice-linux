# RC003-MS 接收层诊断（2026-10-11）

本阶段只定位解码状态和队列补零问题，未替换运行用的 ATVVoice v0.2.0 二进制。

## 已确认的解码状态问题

采集 BlueZ 的 ATVV CTL/RX 通知后，对同一批 120 字节、16 kHz、headerless ADPCM 包执行两种离线策略：沿用上一段 predictor/index；在 AUDIO_START 时重新初始化为 0/0。两种策略均保留 AUDIO_SYNC 的处理，并模拟 ATVVoice v0.2.0 的每包尖峰插值和三点低通，输出增益为 0 dB。

| 真实音频包回放 | 第二段沿用状态的均值 | 第二段重置状态的均值 | AUDIO_SYNC |
| --- | ---: | ---: | ---: |
| 两段各自发送 GET_CAPS | -0.254387 | +0.0000244 | 0 |
| 第二段不重新发送 GET_CAPS | +0.487908 | -0.0001084 | 0 |

均值为归一化 PCM，范围约为 -1～+1。两组都显示：沿用上一段的状态会产生严重直流偏置；重新初始化后的均值接近零。

第二组仍使用同一已连接的遥控器，第二段没有发送 GET_CAPS；中途诊断采集器曾超时退出并重新订阅通知，所以**并非全程不间断的通知订阅**。未对运行中的 ATVVoice 做逐消息插桩。这些限制不影响同一音频包在不同初始状态下的回放结果，但仍需用修正后的原生接收器做连续多次按键验收。

源码对应：

- [v1.0 解码器仅通过 AUDIO_SYNC 重置](https://github.com/b0o/ATVVoice/blob/v0.2.0/src/protocol/v10.rs)。
- [AUDIO_START 仅更新会话状态、序列和计时器](https://github.com/b0o/ATVVoice/blob/v0.2.0/src/atvv.rs)。
- [Vibe Flow 对每个 BeginStream 重新初始化解码器](https://github.com/richlearntodo-debug/vibe-flow/blob/1b56c2fd94267dc02138f28da5bf3f627527fc18/scripts/VibeMicAtvvCapture.cs)。

建议对 RC003-MS 的会话开始加入明确的解码器重置，同时继续遵循后续 AUDIO_SYNC 提供的状态。是否作为通用 ATVV 默认行为，需要其他遥控器的验证；优先限定为本设备兼容选项。

## 队列欠载与补零：高度吻合，缺直接计数

[ATVVoice 的 PipeWire 输出](https://github.com/b0o/ATVVoice/blob/v0.2.0/src/pw.rs) 在 `available < max_samples` 时，直接将剩余输出填零；未设置会话开始预缓冲，也没有欠载计数日志。

上一轮实际 PCM 在持续偏置约 -0.63 的信号之间包含连续数字零区间：13.875、34.5、9.875 ms；另有 0.5625 ms 的短区间。带偏置的波形切到零再切回来，会产生很大的电平跳变。细节见 `artifacts/2026-10-11-receiver-diagnosis/prior-pcm-zero-check.json`。

本轮 D-Bus 回调记录的单包到达间隔最大为 150.0、63.3、162.2、175.2 ms；单包只有 240 个采样，即 15 ms 音频。按固定 16 kHz 消费的简化模型，最大到达迟滞约为 35～202 ms。

限制：这些是主机 D-Bus 回调时间，不是无线电包时间；迟滞模型不是实际 PipeWire 队列深度，也不能证明无线丢包。v1.0 headerless 包没有可用的线上序号，不能把主机侧到达间隔直接称为丢包。因此，当前结论为“队列欠载补零高度吻合”，尚未取得运行接收器的欠载计数证据。

下一阶段应先在队列层记录深度、欠载次数和补零采样数，再用明确的有界预缓冲处理到达抖动。预缓冲会增加延迟，需根据这台设备实测调整，不能用本轮最大间隔直接宣布固定值有效。

## 首尾瞬态仍然存在

不经过 PipeWire、按每次开始重置的直接解码回放，仍在开始约 5～15 ms 和部分录音末尾发现大幅瞬态，峰值接近满幅。原因尚未确认；不能直接归为机械按键声或麦克风启动噪声。

第二段不重新握手的回放中，中途没有超过 0.2 的相邻采样跳变，跳变集中在最初约 15 ms 和最后约 4 ms。接收层重置解决直流偏置后，仍需要处理边界淡入淡出，并由人工试听确认字音未丢失。

## 可复现的工具和证据

- `capture_atvv_packets.py`：使用系统 Python 的 dbus-python/PyGObject，记录原始 CTL/RX 通知；支持按完成会话数自动停止，以及不重新 GET_CAPS。
- `analyze_atvv_packets.py`：使用 NumPy 离线回放，拒绝未完成的采集文件，分别保存 carry/reset WAV 和指标。
- `test_atvv_packets.py`：验证高半字节优先、状态边界、显式重置、AUDIO_SYNC 和每包后处理。

重现第二组分析：

```bash
/home/elma/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 analyze_atvv_packets.py \
  artifacts/2026-10-11-receiver-diagnosis/packets-continuous.jsonl \
  artifacts/2026-10-11-receiver-diagnosis/packets-no-new-caps.jsonl \
  --output-dir artifacts/2026-10-11-receiver-diagnosis/no-new-caps-replay
```

验证：原有 6 项 DSP 测试和新增 4 项诊断测试均通过。此结果不等于 Rust 接收器已修复或真实听感通过。

原始包、回放 WAV 和 JSON 指标保存在 `artifacts/2026-10-11-receiver-diagnosis/`，未提交或外发。采集进程均已退出并释放通知订阅。默认输入输出没有改变。

## 下一阶段建议

优先给原生 ATVVoice 做本设备兼容补丁：会话开始重置、输出前消除直流、队列欠载记录及有界预缓冲、会话边界淡入淡出。先用本轮保存的包验证，再用同一进程连续按键验证，最后进行真人试听。

当前主机未发现 Cargo/Rust 工具链或 libpipewire 的开发 pkg-config 配置。若实施原生补丁，需要先准备隔离的本地构建环境；本轮没有安装构建依赖，没有修改或替换已下载二进制。
