"""Qwen Realtime ASR: one WebSocket per hold, no audio retries or snapshots."""
import asyncio
import base64
from dataclasses import dataclass, field
import json
import os
import re
import time
from urllib.parse import urlencode
import uuid


class CloudASRError(RuntimeError):
    """Safe error text; never include server messages, headers or audio."""


@dataclass(frozen=True)
class QwenConfig:
    api_key: str = field(repr=False)
    region: str = 'beijing'
    model: str = 'qwen3-asr-flash-realtime'
    workspace: str = ''

    @classmethod
    def from_env(cls, region='beijing', model='qwen3-asr-flash-realtime'):
        key = os.environ.get('DASHSCOPE_API_KEY', '').strip()
        workspace = os.environ.get('DASHSCOPE_WORKSPACE_ID', '').strip()
        if not key or '\n' in key or '\r' in key:
            raise ValueError('请在本机终端设置 DASHSCOPE_API_KEY（勿将密钥发到聊天或写入源码）')
        if workspace and not re.fullmatch(r'[A-Za-z0-9-]+', workspace):
            raise ValueError('DASHSCOPE_WORKSPACE_ID 格式不正确')
        if region not in ('beijing', 'singapore'):
            raise ValueError('不支持的 Qwen 地域')
        if not re.fullmatch(r'qwen3-asr-flash-realtime(?:-[0-9-]+)?', model):
            raise ValueError('请使用 qwen3-asr-flash-realtime 或其日期版本')
        return cls(key, region, model, workspace)

    @property
    def url(self):
        if self.workspace:
            region = 'cn-beijing' if self.region == 'beijing' else 'ap-southeast-1'
            host = f'{self.workspace}.{region}.maas.aliyuncs.com'
        else:
            host = 'dashscope.aliyuncs.com' if self.region == 'beijing' else 'dashscope-intl.aliyuncs.com'
        return f'wss://{host}/api-ws/v1/realtime?{urlencode({"model": self.model})}'


class QwenRealtimeSession:
    def __init__(self, config, *, connect=None, timeout=20, audio_processing='off'):
        self.config, self.connect, self.timeout = config, connect, timeout
        if audio_processing not in ('off', 'light'):
            raise ValueError('Unknown audio processing mode')
        self.processor = None
        if audio_processing == 'light':
            from audio_enhancement import SpeechEnhancer
            self.processor = SpeechEnhancer()
        self.processed_audio = bytearray() if self.processor else None
        self.sent_bytes = self.sent_chunks = 0
        self.finish_sent = False

    async def _send(self, ws, kind, **fields):
        event = {'event_id': 'event_'+uuid.uuid4().hex, 'type': kind, **fields}
        await asyncio.wait_for(ws.send(json.dumps(event)), self.timeout)

    async def _receive(self, ws, timeout):
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise CloudASRError('云端返回了无效事件；本句已取消')
        if event['type'] in ('error', 'conversation.item.input_audio_transcription.failed'):
            # Server error strings can echo input, headers or credentials.
            raise CloudASRError('Qwen 云端识别失败，请检查地域、额度、密钥和网络；本句不重传')
        return event

    async def _expect(self, ws, kind):
        event = await self._receive(ws, self.timeout)
        if event['type'] != kind:
            raise CloudASRError('Qwen 会话握手顺序异常；本句已取消')

    async def _upload(self, ws, audio):
        pending = bytearray()
        while True:
            pcm = await audio.get()
            if pcm is None:
                if self.processor:
                    pending.extend(self.processor.finish())
                break
            if self.processor:
                pcm = self.processor.feed(pcm)
            pending.extend(pcm)
            # 100 ms of 16 kHz mono s16le; each byte leaves this buffer once.
            while len(pending) >= 3200:
                await self._append(ws, bytes(pending[:3200]))
                del pending[:3200]
        for pos in range(0, len(pending), 3200):
            await self._append(ws, bytes(pending[pos:pos+3200]))
        self.finish_sent = True
        await self._send(ws, 'session.finish')

    async def _append(self, ws, pcm):
        await self._send(ws, 'input_audio_buffer.append', audio=base64.b64encode(pcm).decode('ascii'))
        if self.processed_audio is not None:
            self.processed_audio.extend(pcm)
        self.sent_bytes += len(pcm)
        self.sent_chunks += 1

    async def _transcripts(self, ws, preview):
        # Dict insertion order follows audio item creation, even if completions
        # arrive out of order. Repeated events replace, rather than append text.
        items, completed = {}, set()
        while True:
            event = await self._receive(ws, 75)
            kind = event['type']
            if kind == 'session.finished':
                if not self.finish_sent or set(items) != completed:
                    raise CloudASRError('Qwen 会话未完整结束；不会粘贴部分文字')
                return ''.join(items.values()).strip()
            if kind in ('input_audio_buffer.speech_started', 'conversation.item.created'):
                item = event.get('item_id') or event.get('item', {}).get('id')
                if item:
                    items.setdefault(item, '')
            if kind not in ('conversation.item.input_audio_transcription.text',
                            'conversation.item.input_audio_transcription.completed'):
                continue
            item = event['item_id']
            if kind.endswith('.completed'):
                items[item] = event['transcript']
                completed.add(item)
            elif item not in completed:
                items[item] = event.get('text', '')+event.get('stash', '')
            preview(''.join(items.values()), self.sent_bytes/32000)

    async def recognize(self, audio, preview):
        started = time.monotonic()
        try:
            if self.connect is None:
                from websockets.asyncio.client import connect
            else:
                connect = self.connect
            headers = {'Authorization': 'Bearer '+self.config.api_key}
            if self.config.workspace:
                headers['X-DashScope-WorkSpace'] = self.config.workspace
            async with connect(self.config.url, additional_headers=headers,
                               open_timeout=self.timeout, close_timeout=3, max_size=2**20) as ws:
                await self._expect(ws, 'session.created')
                await self._send(ws, 'session.update', session={
                    'input_audio_format': 'pcm', 'sample_rate': 16000,
                    'input_audio_transcription': {'language': 'zh'},
                    'turn_detection': {'type': 'server_vad', 'threshold': 0.0,
                                       'silence_duration_ms': 400}})
                await self._expect(ws, 'session.updated')
                upload = asyncio.create_task(self._upload(ws, audio))
                receive = asyncio.create_task(self._transcripts(ws, preview))
                try:
                    done, _ = await asyncio.wait((upload, receive), return_when=asyncio.FIRST_COMPLETED)
                    if receive in done:
                        # Propagate receive failures immediately, even mid-hold.
                        text = receive.result()
                        await upload
                    else:
                        upload.result()
                        text = await asyncio.wait_for(receive, self.timeout)
                finally:
                    for task in (upload, receive):
                        task.cancel()
                    await asyncio.gather(upload, receive, return_exceptions=True)
            return text, {'model': self.config.model, 'backend': 'Qwen Realtime WebSocket',
                          'region': self.config.region, 'audio_seconds': self.sent_bytes/32000,
                          'uploaded_bytes': self.sent_bytes, 'uploaded_chunks': self.sent_chunks,
                          'audio_processing': self.processor.metrics() if self.processor else {'mode': 'off'},
                          'transcription_seconds': time.monotonic()-started}
        except asyncio.CancelledError:
            raise
        except CloudASRError:
            raise
        except Exception:
            # Do not log the original exception/traceback: a transport may echo
            # Authorization or server payloads. Never reconnect or resend audio.
            raise CloudASRError('Qwen 连接失败或超时；本句已取消且不会重传') from None
