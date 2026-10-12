"""Session identity and stale-result protection for push-to-talk previews."""
import time


class TranscriptState:
    def __init__(self):
        self.generation = 0
        self.value = {'phase': 'loading', 'preview': '', 'final': '', 'session': 0,
                      'model': 'Qwen3-ASR 1.7B', 'message': '正在加载本地模型'}
        self.events = []
        self.started = None

    def begin(self, now=None):
        self.generation += 1
        self.started = time.monotonic() if now is None else now
        self.events = []
        self.value.update(phase='recording', preview='', final='', session=self.generation,
                          first_preview_seconds=None, metrics={}, message='正在收音，预览文字会随语音修正')
        return self.generation

    def release(self):
        self.value.update(phase='tail', message='收音结束，正在保留尾音')

    def cancel(self, message):
        self.generation += 1
        self.value.update(phase='cancelled', preview='', final='', message=message)

    def preview(self, generation, text, metrics, snapshot_seconds, now=None):
        if generation != self.generation or self.value['phase'] != 'recording':
            return False
        if not text.strip() or text == self.value['preview']:
            return False
        now = time.monotonic() if now is None else now
        elapsed = now-self.started
        self.value.update(preview=text, message='预览中；松开后生成最终文字')
        if self.value['first_preview_seconds'] is None:
            self.value['first_preview_seconds'] = elapsed
        self.events.append({'since_press_seconds': elapsed, 'audio_snapshot_seconds': snapshot_seconds,
                            'text': text, **metrics})
        return True

    def finalizing(self):
        self.value.update(phase='finalizing', message='正在生成最终文字，请保持目标输入框焦点')

    def finish(self, generation, text, status, metrics):
        if generation != self.generation:
            return False
        self.value.update(phase=status, preview='', final=text, metrics=metrics,
                          message='最终文字已发送到输入框' if status == 'paste_shortcut_sent'
                          else ('没有检测到语音' if status == 'no_speech' else '最终文字已生成'))
        return True
