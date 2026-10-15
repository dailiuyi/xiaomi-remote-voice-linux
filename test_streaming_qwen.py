import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch
import wave

from dictation_core import CapturePhase
from streaming_dictation import CloudStreamingEngine, run
from streaming_qwen import CloudASRError, QwenConfig, QwenRealtimeSession


PREFIX = 'conversation.item.input_audio_transcription.'
CONFIG = QwenConfig('test-key-not-a-credential')


class ConfigTests(unittest.TestCase):
    def test_missing_key_fails_without_exposing_values(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'DASHSCOPE_API_KEY'):
                QwenConfig.from_env()
        self.assertNotIn(CONFIG.api_key, repr(CONFIG))

    def test_regions_workspaces_and_model_query(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': CONFIG.api_key,
                                     'DASHSCOPE_WORKSPACE_ID': 'workspace-123'}, clear=True):
            config = QwenConfig.from_env('singapore')
            self.assertEqual(config.url, 'wss://workspace-123.ap-southeast-1.maas.aliyuncs.com/'
                             'api-ws/v1/realtime?model=qwen3-asr-flash-realtime')
        self.assertIn('dashscope.aliyuncs.com', CONFIG.url)
        self.assertIn('dashscope-intl.aliyuncs.com', QwenConfig(CONFIG.api_key, 'singapore').url)

    def test_workspace_cannot_redirect_credentials(self):
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': CONFIG.api_key,
                                     'DASHSCOPE_WORKSPACE_ID': 'bad.example/path'}, clear=True):
            with self.assertRaises(ValueError):
                QwenConfig.from_env()


class Socket:
    """Server event script with append receipts; never contacts a provider."""
    def __init__(self, finish_events=(), on_append=(), handshake='session.updated'):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({'type': 'session.created'})
        self.finish_events, self.on_append, self.handshake = finish_events, on_append, handshake
        self.events, self.closed = [], False

    async def send(self, payload):
        event = json.loads(payload)
        self.events.append(event)
        if event['type'] == 'session.update':
            self.incoming.put_nowait({'type': self.handshake})
        elif event['type'] == 'input_audio_buffer.append':
            for response in self.on_append:
                self.incoming.put_nowait(response)
            self.on_append = ()
        elif event['type'] == 'session.finish':
            for response in self.finish_events:
                self.incoming.put_nowait(response)

    async def recv(self):
        event = await self.incoming.get()
        if isinstance(event, Exception):
            raise event
        return json.dumps(event)

    @asynccontextmanager
    async def connect(self, url, **kwargs):
        self.url, self.options = url, kwargs
        try:
            yield self
        finally:
            self.closed = True

    def audio(self):
        return b''.join(base64.b64decode(e['audio']) for e in self.events
                        if e['type'] == 'input_audio_buffer.append')


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_enhancement_flush_is_exact_once_and_never_oversized(self):
        for size in (0, 1, 159, 1759, 3519):
            with self.subTest(size=size):
                socket = Socket(finish_events=[{'type': 'session.finished'}])
                queue = asyncio.Queue()
                pcm = b'\xff\x7f' * size
                for pos in range(0, len(pcm), 512):
                    queue.put_nowait(pcm[pos:pos+512])
                queue.put_nowait(None)
                session = QwenRealtimeSession(CONFIG, connect=socket.connect, audio_processing='light')
                _, metrics = await session.recognize(queue, lambda *_: None)
                self.assertEqual(len(socket.audio()), len(pcm))
                self.assertEqual(socket.audio(), session.processed_audio)
                self.assertEqual(metrics['audio_processing']['input_samples'], size)
                self.assertEqual(metrics['audio_processing']['output_samples'], size)
                self.assertEqual(metrics['uploaded_bytes'], len(pcm))
                self.assertTrue(all(len(base64.b64decode(e['audio'])) <= 3200 for e in socket.events if 'audio' in e))
                self.assertEqual(sum(e['type'] == 'session.finish' for e in socket.events), 1)
                if size:
                    self.assertNotEqual(socket.audio(), pcm)  # Peak limit is active.

    async def recognize(self, socket, pcm=b'\x01\x00'*3500):
        queue = asyncio.Queue()
        for pos in range(0, len(pcm), 512):
            queue.put_nowait(pcm[pos:pos+512])
        queue.put_nowait(None)
        previews = []
        session = QwenRealtimeSession(CONFIG, connect=socket.connect, timeout=.1)
        result = await session.recognize(queue, lambda text, seconds: previews.append(text))
        return result, previews

    async def test_pcm_sent_once_with_tail_and_duplicate_completed_deduplicated(self):
        socket = Socket(finish_events=[
            {'type': PREFIX+'completed', 'item_id': 'one', 'transcript': '最终文字'},
            {'type': PREFIX+'completed', 'item_id': 'one', 'transcript': '最终文字'},
            {'type': 'session.finished'}], on_append=[
            {'type': PREFIX+'text', 'item_id': 'one', 'text': '最终', 'stash': '草稿'}])
        pcm = bytes(range(256))*27 + b'\x00\x01'
        (text, metrics), previews = await self.recognize(socket, pcm)
        self.assertEqual(socket.audio(), pcm)
        self.assertEqual(metrics['uploaded_bytes'], len(pcm))
        self.assertEqual(text, '最终文字')
        self.assertIn('最终草稿', previews)
        kinds = [event['type'] for event in socket.events]
        self.assertEqual(kinds.count('session.finish'), 1)
        self.assertNotIn('input_audio_buffer.commit', kinds)
        self.assertTrue(all(len(base64.b64decode(e['audio'])) <= 3200 for e in socket.events if 'audio' in e))
        ids = [event['event_id'] for event in socket.events]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(socket.closed)

    async def test_multiple_sentences_keep_audio_order_even_if_completions_reverse(self):
        socket = Socket(on_append=[
            {'type': 'input_audio_buffer.speech_started', 'item_id': 'first'},
            {'type': 'input_audio_buffer.speech_started', 'item_id': 'second'}], finish_events=[
            {'type': PREFIX+'completed', 'item_id': 'second', 'transcript': '第二句。'},
            {'type': PREFIX+'completed', 'item_id': 'first', 'transcript': '第一句。'},
            {'type': 'session.finished'}])
        (text, _), _ = await self.recognize(socket)
        self.assertEqual(text, '第一句。第二句。')

    async def test_silence_is_empty_final(self):
        (text, _), _ = await self.recognize(Socket(finish_events=[{'type': 'session.finished'}]))
        self.assertEqual(text, '')

    async def test_partial_or_incomplete_session_cannot_be_final(self):
        cases = [
            [{'type': PREFIX+'text', 'item_id': 'one', 'text': '', 'stash': '草稿'},
             {'type': 'session.finished'}],
            [ConnectionError('Authorization: Bearer do-not-log')],
            [],  # No session.finished: bounded final timeout.
        ]
        for events in cases:
            with self.subTest(events=events):
                socket = Socket(finish_events=events)
                with self.assertRaises(CloudASRError) as error:
                    await self.recognize(socket)
                self.assertNotIn('do-not-log', str(error.exception))
                self.assertTrue(socket.closed)
                self.assertEqual(len(socket.audio()), 7000)

    async def test_errors_mid_hold_abort_without_waiting_for_release_or_retry(self):
        for kind in ('error', PREFIX+'failed'):
            socket = Socket(on_append=[{'type': kind, 'error': {'message': 'sensitive-secret'}}])
            queue = asyncio.Queue()
            queue.put_nowait(b'\x01\x00'*1600)
            session = QwenRealtimeSession(CONFIG, connect=socket.connect, timeout=.1)
            with self.assertRaises(CloudASRError) as error:
                await asyncio.wait_for(session.recognize(queue, lambda *_: None), 1)
            self.assertNotIn('sensitive-secret', str(error.exception))
            self.assertTrue(socket.closed)
            self.assertEqual(len(socket.audio()), 3200)

    async def test_handshake_failure_uploads_no_audio(self):
        socket = Socket(handshake='error')
        with self.assertRaises(CloudASRError):
            await self.recognize(socket)
        self.assertEqual(socket.audio(), b'')

    async def test_cancellation_closes_socket_and_discards_partial(self):
        socket = Socket()
        task = asyncio.create_task(QwenRealtimeSession(CONFIG, connect=socket.connect).recognize(
            asyncio.Queue(), lambda *_: None))
        for _ in range(30):
            if socket.events:
                break
            await asyncio.sleep(.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(socket.closed)
        self.assertEqual(socket.audio(), b'')


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.runtime_patch = patch('streaming_dictation.RUNTIME', self.directory)
        self.runtime_patch.start()
        self.notify_patch = patch('streaming_dictation.notify')
        self.notify_patch.start()
        self.engines, self.tasks = [], []

    async def asyncTearDown(self):
        for engine in self.engines:
            engine.stop.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for engine in self.engines:
            await engine.close()
        self.runtime_patch.stop()
        self.notify_patch.stop()
        self.temp.cleanup()

    def engine(self, socket, audio_processing='off'):
        updates = []
        engine = CloudStreamingEngine(CONFIG, lambda state: updates.append(dict(state)),
            lambda: QwenRealtimeSession(CONFIG, connect=socket.connect, timeout=.1,
                                       audio_processing=audio_processing))
        self.assertEqual(engine.controller.tail_seconds, .3)
        engine.controller.tail_seconds = 0
        self.engines.append(engine)
        return engine, updates

    async def finalize(self, engine):
        paste = AsyncMock()
        self.tasks += [asyncio.create_task(engine.previews()), asyncio.create_task(engine.tails()),
                       asyncio.create_task(engine.finals(self.directory, paste))]
        engine.state_changed('connected')
        for _ in range(200):
            if engine.controller.phase == CapturePhase.IDLE:
                break
            await asyncio.sleep(.005)
        self.assertEqual(engine.controller.phase, CapturePhase.IDLE)
        await asyncio.wait_for(engine.jobs.join(), 1)
        return paste

    async def test_live_upload_before_release_and_single_paste_after_finished(self):
        socket = Socket(on_append=[{'type': PREFIX+'text', 'item_id': 'one', 'text': '', 'stash': '预览'}],
            finish_events=[{'type': PREFIX+'completed', 'item_id': 'one', 'transcript': '最终文字'},
                           {'type': 'session.finished'}])
        engine, updates = self.engine(socket)
        engine.feed(b'\x7f\x00'*1600)  # Idle PCM is never sent.
        engine.state_changed('streaming')
        pcm = b'\x01\x00'*1600
        engine.feed(pcm)
        engine.state_changed('streaming')  # Duplicate press cannot restart/upload.
        for _ in range(100):
            if any(s['preview'] == '预览' for s in updates):
                break
            await asyncio.sleep(.005)
        self.assertTrue(any(s['preview'] == '预览' for s in updates))
        self.assertEqual(socket.audio(), pcm)  # Already streamed while holding.
        paste = await self.finalize(engine)
        paste.assert_awaited_once_with('最终文字')
        self.assertEqual(socket.audio(), pcm)
        self.assertEqual(engine.state.value['metrics']['uploaded_bytes'], len(pcm))

    async def test_failed_final_never_pastes_preview(self):
        socket = Socket(finish_events=[{'type': PREFIX+'failed', 'error': {'message': 'secret'}}])
        engine, _ = self.engine(socket)
        engine.state_changed('streaming')
        engine.feed(b'\x01\x00'*1600)
        with self.assertLogs('streaming', level='ERROR'):
            paste = await self.finalize(engine)
        paste.assert_not_awaited()
        self.assertEqual(engine.state.value['phase'], 'failed')
        self.assertFalse((self.directory/'last-transcript.txt').exists())

    async def test_enhanced_session_preserves_raw_and_saves_exact_sent_audio(self):
        socket = Socket(finish_events=[
            {'type': PREFIX+'completed', 'item_id': 'one', 'transcript': '增强结果'},
            {'type': 'session.finished'}])
        engine, _ = self.engine(socket, audio_processing='light')
        engine.feed(b'\xff\x7f'*1600)  # Idle PCM must not train the processor.
        engine.state_changed('streaming')
        pcm = b'\xff\x7f'*1759
        engine.feed(pcm)
        paste = await self.finalize(engine)
        paste.assert_awaited_once_with('增强结果')
        with wave.open(str(self.directory/'001.wav'), 'rb') as audio:
            self.assertEqual(audio.readframes(audio.getnframes()), pcm)
        with wave.open(str(self.directory/'001.processed.wav'), 'rb') as audio:
            self.assertEqual(audio.readframes(audio.getnframes()), socket.audio())
        receipt = json.loads((self.directory/'001.json').read_text())
        self.assertEqual(receipt['audio_processing']['mode'], 'light')
        self.assertEqual(receipt['uploaded_bytes'], len(pcm))
        self.assertEqual(receipt['audio_processing']['input_samples'], 1759)

    async def test_enhancement_does_not_reuse_state_across_holds_or_flush_cancelled_audio(self):
        socket = Socket()
        engine, _ = self.engine(socket, audio_processing='light')
        engine.state_changed('streaming')
        first = engine.cloud_session
        engine.feed(b'\xff\x7f'*1759)
        engine.state_changed('disconnected')
        await asyncio.gather(engine.cloud_task, return_exceptions=True)
        self.assertFalse(first.finish_sent)
        self.assertEqual(socket.audio(), b'')
        engine.state_changed('streaming')
        second = engine.cloud_session
        self.assertIsNot(first.processor, second.processor)
        self.assertEqual(second.processor.input_samples, 0)
        self.assertEqual(second.processor.gain_db, 0)

    async def test_disconnect_overlap_and_duration_cancel_connections(self):
        for reason in ('disconnect', 'overlap', 'duration'):
            with self.subTest(reason=reason):
                socket = Socket()
                engine, _ = self.engine(socket)
                engine.state_changed('streaming')
                engine.feed(b'\x01\x00'*1600)
                if reason == 'disconnect':
                    engine.state_changed('disconnected')
                elif reason == 'overlap':
                    engine.state_changed('connected')
                    engine.state_changed('streaming')
                else:
                    engine.controller.max_bytes = 3200
                    engine.feed(b'\x00\x00')
                await asyncio.gather(engine.cloud_task, return_exceptions=True)
                self.assertEqual(engine.controller.phase, CapturePhase.IDLE)
                self.assertEqual(engine.state.value['preview'], '')
                self.assertTrue(engine.jobs.empty())

    async def test_backlog_cancels_and_requires_release(self):
        engine, _ = self.engine(Socket())
        engine.state_changed('streaming')
        engine.audio_queue = asyncio.Queue(maxsize=1)
        engine.feed(b'\x01\x00')
        engine.feed(b'\x02\x00')
        self.assertEqual(engine.controller.phase, CapturePhase.IDLE)
        self.assertEqual(engine.state.value['phase'], 'failed')
        engine.state_changed('streaming')
        self.assertEqual(engine.controller.phase, CapturePhase.IDLE)
        with patch('streaming_dictation.notify'):
            engine.state_changed('connected')
            engine.state_changed('streaming')
        self.assertEqual(engine.controller.phase, CapturePhase.RECORDING)

    async def test_remote_error_mid_hold_stops_capture_until_release(self):
        socket = Socket(on_append=[{'type': 'error'}])
        engine, _ = self.engine(socket)
        engine.state_changed('streaming')
        engine.feed(b'\x01\x00'*1600)
        with self.assertLogs('streaming', level='ERROR'):
            await asyncio.gather(engine.cloud_task, return_exceptions=True)
        self.assertEqual(engine.controller.phase, CapturePhase.IDLE)
        self.assertEqual(engine.state.value['phase'], 'failed')
        self.assertTrue(engine.controller.ignored_hold)
        engine.state_changed('connected')
        self.assertFalse(engine.controller.ignored_hold)

    async def test_cloud_replay_full_lifecycle_without_model_or_keyboard(self):
        pcm = b'\x01\x00'*1600
        source = self.directory/'synthetic.wav'
        with wave.open(str(source), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(pcm)
        socket = Socket(finish_events=[
            {'type': PREFIX+'completed', 'item_id': 'one', 'transcript': '回放结果'},
            {'type': 'session.finished'}])
        session = QwenRealtimeSession(CONFIG, connect=socket.connect)
        args = SimpleNamespace(backend='qwen-api', qwen_config=CONFIG, replay=source, audio_processing='off')
        panel = SimpleNamespace(publish=lambda _: None, url='http://127.0.0.1/test/')
        with patch('streaming_qwen.QwenRealtimeSession', return_value=session), \
             patch('streaming_dictation.ReceiverSession.start', new_callable=AsyncMock) as receiver, \
             patch('streaming_dictation.DesktopPaste.paste', new_callable=AsyncMock) as keyboard:
            await asyncio.wait_for(run(args, panel, None), 4)
        receiver.assert_not_awaited()
        keyboard.assert_not_awaited()
        self.assertEqual(socket.audio(), pcm)
        self.assertEqual((self.directory/'last-replay-transcript.txt').read_text(), '回放结果\n')
        metadata = json.loads((self.directory/'replay-current.json').read_text())
        self.assertEqual(metadata['backend'], 'qwen-api')
        self.assertEqual(metadata['tail_ms'], 300)
        self.assertNotIn(CONFIG.api_key, json.dumps(metadata))
        self.assertNotIn('streaming_asr', sys.modules)
        self.assertNotIn('torch', sys.modules)


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_loopback_websocket_headers_and_audio(self):
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve
        captured = bytearray()

        async def server(ws):
            self.assertEqual(ws.request.headers['Authorization'], 'Bearer '+CONFIG.api_key)
            await ws.send(json.dumps({'type': 'session.created'}))
            update = json.loads(await ws.recv())
            self.assertEqual(update['session']['sample_rate'], 16000)
            self.assertEqual(update['session']['turn_detection']['type'], 'server_vad')
            await ws.send(json.dumps({'type': 'session.updated'}))
            async for payload in ws:
                event = json.loads(payload)
                if event['type'] == 'input_audio_buffer.append':
                    captured.extend(base64.b64decode(event['audio']))
                elif event['type'] == 'session.finish':
                    await ws.send(json.dumps({'type': PREFIX+'completed', 'item_id': 'one', 'transcript': '本机模拟'}))
                    await ws.send(json.dumps({'type': 'session.finished'}))
                    break

        async with serve(server, '127.0.0.1', 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            def local_connect(url, **kwargs):
                return connect(f'ws://127.0.0.1:{port}/', proxy=None, **kwargs)
            audio = asyncio.Queue()
            pcm = bytes(range(256))*30
            audio.put_nowait(pcm)
            audio.put_nowait(None)
            text, metrics = await QwenRealtimeSession(CONFIG, connect=local_connect).recognize(audio, lambda *_: None)
            self.assertEqual(text, '本机模拟')
            self.assertEqual(captured, pcm)
            self.assertEqual(metrics['uploaded_bytes'], len(pcm))


if __name__ == '__main__':
    unittest.main()
