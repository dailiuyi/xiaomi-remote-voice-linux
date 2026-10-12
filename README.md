# Xiaomi RC003-MS 语音输入

目前已接通：**按住语音键收音 → 松开后本地转写 → 粘贴到当前焦点输入框**。2026-10-12 已完成真实遥控器和 Codex 内置浏览器输入框验收，用户反馈“文字自动出现”。程序只发送 Ctrl+V，不发送 Enter。

启动前按一下方向键唤醒遥控器，然后在终端运行：

```bash
bash /home/elma/文档/ChatGPT/Mi遥控/start_dictation.sh
```

看到 `READY` 后点击目标输入框，按住语音键说话，松开后等待粘贴。退出使用 Ctrl+C。配置、验收指标、限制及录音存储说明见 [语音输入使用说明](DICTATION.md)。未设置开机自启，未提交或推送。

最新原生接收器已完成两次连续按键复测和人工试听，用户反馈“明显改善，人声完整”；实现与证据边界见 [接收器修复](RECEIVER_FIX.md)。以下内容保留上一轮 DSP 测试记录。

接收层检查过程见 [接收层诊断](RECEIVER_DIAGNOSIS.md)，修复结果以 [接收器修复](RECEIVER_FIX.md) 为准。以下是早期 DSP 测试的历史记录，其中尚未修复的描述不代表当前接收器状态。

## 历史 DSP 结论（2026-10-11）

- 已实现并实测实时 DSP 链路，6 项合成信号测试通过。
- 高通消除了大部分直流偏置。固定噪声谱估计用于持续底噪抑制，不自动提高增益。
- 最新真实录音仍有短尖峰，不能宣布爆音修复完成；音质、字音保留和可用性等待人工试听。
- 两次按键期间，原声的稳定电平分别约为 -0.59、-0.64（归一化 PCM），中途出现多次接近零电平的短暂区间及突然跳变。处理后整段均值从 -0.511 降至 -0.000029。
- 上述均值变化是直流偏置消除的证据，**不是语音降噪量**。原声与处理版的整段 RMS 差不能当作信噪比改善。
- 最新处理版仍有 40 个相邻采样差超过 0.2 的跳变，峰值约 -2.99 dBFS；未出现满幅采样。没有满幅采样不代表没有爆音。

## 实现

`audio_filter.py` 使用 NumPy，输入输出为 16 kHz、单声道、signed 16-bit PCM：

1. 新按键会话的 32 ms 淡入，仅在至少 64 ms 数字零静默之后触发。
2. 80 Hz 一阶高通，抑制直流和低频偏置。
3. 五点中值检测，替换孤立的大幅异常采样。
4. 512 点 STFT、256 点步长，以明确选定的按住静默区间估计固定噪声谱；保守谱衰减、时间和频率平滑。
5. 固定输出增益，软限幅上界 0.82。

STFT 算法延迟为 16 ms；尚未测量蓝牙到应用端的总延迟。滤波器不修改 ATVVoice 二进制。

测试运行时：Python 3.12 / NumPy 2.3.5。测试命令：

```bash
/home/elma/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m unittest -v test_audio_filter
```

测试覆盖平稳噪声衰减、600/1700 Hz 合成信号保留、孤立尖峰和过载、数字静默、起始瞬态、分块与离线输出一致性。合成信号不能证明真实人声的听感。

## 录音与证据

文件均在 `artifacts/2026-10-11-filter-v1/`，不纳入 Git：

- `comparison-raw.wav` / `comparison-cleaned.wav`：最后一轮同时采集的原声、实时处理结果，含两次按键。两者分别裁掉等待静默，**没有做采样级时间对齐**。
- `live-v2-raw.wav` / `live-v2-cleaned.wav`：包含等待时间的完整录音。
- `final-live-check.json`：最后一轮指标及限制。
- `final-cleaned.profile.json`：较早录音起始 0.25–1.50 秒指定静默区间得到的噪声谱。
- `final-cleaned.report.json`：该指定区间的 RMS 下降约 22.18 dB，包含直流和低频消除；不可推广为任何环境下的降噪量，也不是最后一轮的静默指标。
- `*.service.log`：本轮接收器及实时滤波服务日志。
- `cleanup-check.json`：临时虚拟声卡和麦克风已卸载，默认输入输出与测试前一致。

## 下一步诊断边界

已确认原声存在明显偏置和短时跳变。原因尚未确认。需要优先检查 ATVVoice 的每次 AUDIO_START 解码状态、控制消息与音频的先后次序，以及 PipeWire 队列欠载补零。

对照源码：

- [Vibe Flow 的 ATVV 接收实现](https://github.com/richlearntodo-debug/vibe-flow/blob/1b56c2fd94267dc02138f28da5bf3f627527fc18/scripts/VibeMicAtvvCapture.cs)：每次 BeginStream 重置解码状态。
- [ATVVoice v0.2.0 的 v1.0 协议实现](https://github.com/b0o/ATVVoice/blob/v0.2.0/src/protocol/v10.rs)：解码状态重置依赖 AUDIO_SYNC。当前测试日志没有 AUDIO_SYNC 记录。

这只是排查方向，尚未证明重置差异或补零就是本设备的根因。进一步修正接收层后，需要新的设备录音验证。

## 收尾状态

本轮 transient user services 已停止，创建的两个 PulseAudio 兼容模块已卸载，未设置开机自启。源码和录音已保存，未提交或推送。没有修改系统默认麦克风和扬声器。
