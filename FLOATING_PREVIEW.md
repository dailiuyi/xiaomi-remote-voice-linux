# MI 语音灵动岛悬浮预览

2026-10-12，elma-ubuntu-pc，Ubuntu KDE Wayland。已升级为高仿苹果灵动岛（Dynamic Island）流体动效与外观形态。

## 视觉与动效特性（Apple Dynamic Island）

1. **纯黑胶囊外观与微光高光**：
   - 背景采用苹果纯深黑（`#08080a`），四周具有 1px 细微高光描边（`rgba(255, 255, 255, 0.11)`），摆脱普通深灰矩形卡片感。
   - 收起态为紧凑小药丸（210×38 像素，全圆角 19px），左侧显示就绪/录音状态光点，中间显示紧凑标题，右侧具有微型状态条。
   - 展开态平滑形变成灵动岛展开体（430×138 像素，圆角 26px）。
2. **Fluid Spring 弹性阻尼物理动效**：
   - 展开与收缩采用 `QVariantAnimation` 配合 `QEasingCurve.Type.OutBack` 弹性曲线（约 5% Overshoot 微回弹），模拟 iOS 原生流体弹簧手感，时长 320ms。
   - 展开时以中心锚点向两翼平滑对称膨胀拉伸，文字伴随透明度渐显，收缩时流畅缩回小胶囊。
3. **灵动岛声波与状态微动效**：
   - **收音中（Recording）**：左上角展现 4 柱跳动的 iOS 橙色动态音频波形（Waveform Bars，35ms 律动起伏）。
   - **生成中（Finalizing）**：波形平息，提示“正在生成最终文字”。
   - **输入完成（Paste Sent）**：左侧点亮绿色苹果对勾微标，展示最终文本 0.8 秒后即刻启动 180ms 弹性阻尼回弹，干脆利落收缩回小胶囊。
4. **定位与交互**：
   - **主屏幕绑定**：始终优先绑定系统主屏幕（`primaryScreen`，如外接显示器 HDMI-A-1）；自动监听多屏插拔与主屏切换，避免遗留在副屏或笔记本内置屏。
   - **默认吸顶居中**：在当前主屏幕顶部居中悬浮（Top-Center，距离屏幕顶部边缘 14 逻辑像素），还原灵动岛吸顶感。
   - **自由拖拽**：鼠标左键按住胶囊即可随意拖拽至屏幕任意位置（鼠标悬停显示移动十字光标）；仅在鼠标松开时持久化相对主屏幕坐标至 `artifacts/floating-preview/position.json`，不会因窗口初始化误落盘。
   - **双击复位**：双击胶囊任何非按钮区域，立即一键重置回“当前主屏幕顶部正中央”。
   - **关闭按钮**：右上角隐蔽的微型“×”按钮（悬停微亮），点击只关闭浮窗，不中断后台听写服务。

## 使用

### 1. 桌面快捷方式（一键开关）

桌面上已创建 **「MI 语音输入」**（`mi-voice.desktop`）：
- **点击一次**：若服务未运行，自动启动语音输入后台与灵动岛悬浮窗，右下角弹出启动提示。
- **再次点击**：若服务已运行，自动安全关闭语音服务与悬浮窗，右下角弹出关闭提示。

### 2. 命令行控制

保持原有精细命令：

```bash
# 一键切换开/关（桌面快捷方式同等脚本）
bash /home/elma/文档/ChatGPT/Mi遥控/toggle_dictation.sh

# 显示灵动岛悬浮窗（后台 Qwen 服务会自动复用）
bash /home/elma/文档/ChatGPT/Mi遥控/start_floating_preview.sh

# 关闭悬浮窗（识别和输入继续运行）
bash /home/elma/文档/ChatGPT/Mi遥控/start_floating_preview.sh --stop

# 启动整套流式语音听写（含灵动岛浮窗）
bash /home/elma/文档/ChatGPT/Mi遥控/start_streaming_dictation.sh

# 停止全部服务与悬浮窗
bash /home/elma/文档/ChatGPT/Mi遥控/start_streaming_dictation.sh --stop
```

## 验证与证据

- 单元测试：`python3 test_floating_preview.py` 通过（覆盖 URL 校验、展开/收缩尺寸、状态机过渡、双击复位）。
- 实时状态证据：`artifacts/floating-preview/current.json`。
- 视觉渲染抓取：
  - 常态紧凑胶囊：`artifacts/floating-preview/last-compact.png`
  - 录音跳动波形展开态：`artifacts/floating-preview/last-preview-recording.png`
  - 最终输入完成态：`artifacts/floating-preview/last-preview.png`
- 安全边界：坚持无焦点属性、强制置顶、单实例文件锁与 pidfd 校验。
- 任务栏隔离：窗口使用 `Qt.WindowType.ToolTip` 与 EWMH `_NET_WM_STATE_SKIP_TASKBAR` / `_NET_WM_STATE_SKIP_PAGER`，确保不出现在任务栏与 Alt-Tab 切换器中。
