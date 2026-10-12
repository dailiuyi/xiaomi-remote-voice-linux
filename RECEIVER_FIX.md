# RC003-MS 原生接收器修复（2026-10-12）

用户已同意实施接收层修复及连续按键、人工试听验收。源码位于 `receiver/atvvoice`，基于 b0o/ATVVoice v0.2.0；原运行二进制保留。当前仅本地编译和临时测试，未安装、自启、提交或推送，尚未接入语音识别。

## 修改范围

显式启用 `--rc003-ms`，校验协商结果为 ATVV v1.0、16 kHz、120 字节；每次 AUDIO_START 重置 ADPCM predictor/index 和内部序号，后续 AUDIO_SYNC 仍可覆盖状态。普通模式保持原来的重置策略。

兼容模式在输出前连续执行 80 Hz 直流消除；默认 220 ms 预缓冲，32 ms 淡入、16 ms 尾部保留及淡出。欠载时淡出、重新预缓冲，并计数。增益测试固定 0 dB，软限幅上限 0.82。该模块处理接收边界和低频偏置，持续宽带底噪仍由既有 `audio_filter.py` 的谱降噪负责。

内部条件化队列上限 500 ms；PCM 传输容量 16 事件，上游 frame 通道 64 事件，会话待发送队列 64 事件。**500 ms 仅为条件化模块的上限，不是整条流水线的总缓存或端到端延迟。**输出背压等待纳入 select，以保持松开按键及断连控制响应；STOP 前接收已经就绪的 RX 包。不同 BlueZ socket 上尚未就绪的晚到尾帧仍需实测。

正常退出先有界等待转发，再请求音频线程确认传输队列与内部队列已排空。900 ms 为输出确认截止时间，并非播放成功证据；超时关闭会记录未播放样本。强制取消或 BLE 错误可能无法完成有序排空，局部待发送队列析构时记录截断事件数和已解码样本数。自然断连的待发送队列使用整体 2 秒截止时间。未在本轮进行真实输出暂停/故障注入验收。

## 本地验证

- 115 项 Rust 测试通过，包含跨按键重置、AUDIO_SYNC 优先、默认行为、连续高通、边界、短段、欠载、缓存上限、传输顺序，以及输出堵塞时 STOP/断连控制响应。
- 原有 10 项 Python DSP 和诊断回放测试通过。
- 原始设备包在原生算法中回放，两组模拟的欠载/丢弃计数均为 0，已收到 PCM 全部排空。该结果是固定 256 样本回调的模拟，不能代替真实 PipeWire。
- 回放处理后的相邻采样差超过 0.2 仍分别有 1、7 次；不能宣布爆音彻底解决。
- 独立源码复查报告：`artifacts/receiver-build/review.md`。日志：`test-final.log`、`build-final.log`。

## 编译和临时运行

构建脚本 `build_receiver.sh` 使用 `artifacts/receiver-build` 中的独立 Rust 工具链和开发库 sysroot，没有 apt 安装或 sudo 操作：

```bash
bash build_receiver.sh test --locked
bash build_receiver.sh build --locked
artifacts/receiver-build/target/debug/atvvoice \
  --device C0:5D:39:C2:B1:79 --name rc003-candidate \
  --rc003-ms --prebuffer-ms 220 --gain 0 --frame-timeout 0 \
  --keep-alive 10 --no-dbus -v
```

临时测试由 `mi-remote-native-test.service` 运行，最长 15 分钟，退出信号 SIGINT。使用专属 PipeWire 源 `atvvoice-rc003-candidate`；未改变系统默认麦克风/扬声器。真实录音及候选 SHA 位于 `artifacts/2026-10-12-native-receiver`。当前采集候选和最新构建仅退出截断记账、统一排空截止时间不同；音频算法一致。仍须分别报告实际测试的版本，不能称最新构建已完成所有设备验收。

## 设备复测与人工验收

同一条蓝牙连接上的两次按住收音；检查真实欠载/丢弃/排空计数、每段偏置，以及人声首尾、底噪和爆音的人工试听。两次真实按键采集已完成：00:24:47–00:24:56、00:25:00–00:25:08（Asia/Shanghai）；同一连接只有一次 CAPS 协商。欠载、missing、模块丢弃、interrupted 均为 0；累计提交 PipeWire 260880 个 PCM 样本，两次松开后队列归零，退出 Drain 确认。真实录音两段均值分别约 +0.0000115、+0.00000327；相邻采样差超过 0.2 均为 0，峰值约 -14.48、-14.10 dBFS，没有满幅样本。这些指标不能证明所有可听爆音已消除，也不能证明无线零丢包。

已从同一次录音导出 `live-native-comparison.wav` 和 `live-denoised-comparison.wav`，长度约 20.9 秒；后者是离线套用已有谱降噪，并补偿算法延迟，本轮没有运行实时 DSP 服务。噪声谱取第一段按住后的 0.3–1.6 秒，按测试指令假设为静默；该区间 RMS 约 -57.43→-64.49 dBFS，下降 7.06 dB，不能作为语音信噪比。第二段对应区间电平较高，可能包含讲话，不能作为独立降噪验证。

用户已完成对照试听，反馈“明显改善，人声完整”。本轮接收层修复的连续按键设备复测与整体人工试听通过；用户未指定两个版本中的偏好。此结果不代表语音识别、文字输入或长期运行验收。两个临时 unit 和录音进程均已结束，测试源已移除，系统默认输入输出未变；证据为 `live-check.json`、`live-native.service.log`、`cleanup-check.json`。保留实际测试二进制 `atvvoice-tested`，SHA 对应 09bcbba；最新构建 484f119 的额外退出记账改动只有本地测试与源码复查，未冒称其已完成设备验收。
