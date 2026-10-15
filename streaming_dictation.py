#!/usr/bin/env python3
"""Qwen cloud/local push-to-talk previews, followed by one final focus paste."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import select
import subprocess
import time
import wave

from dictation import BUS_NAME, OBJECT_PATH, SOURCE, ROOT, RUNTIME as LEGACY_RUNTIME
from dictation import DesktopPaste, notify, receiver_target, terminate, write_pcm
from dictation_core import CaptureController, CapturePhase
from streaming_core import TranscriptState
from streaming_preview import PreviewServer

RUNTIME = ROOT/'artifacts/streaming-runtime'
LOG = logging.getLogger('streaming')


def stop_current(metadata_path=RUNTIME/'current.json'):
    if not metadata_path.exists():
        print('No streaming instance recorded')
        return
    try:
        pid = int(json.loads(metadata_path.read_text())['pid'])
    except Exception:
        metadata_path.unlink(missing_ok=True)
        return
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        print('Recorded streaming instance already stopped')
        metadata_path.unlink(missing_ok=True)
        return
    try:
        proc = Path(f'/proc/{pid}')
        if proc.stat().st_uid != os.getuid() or str(ROOT/'streaming_dictation.py').encode() not in (proc/'cmdline').read_bytes().split(b'\x00'):
            raise RuntimeError('Recorded PID does not match this streaming program; no signal sent')
        signal.pidfd_send_signal(descriptor, signal.SIGINT)
        if not select.select([descriptor], [], [], 10)[0]:
            raise RuntimeError('Stop requested, but exit not confirmed; no forced termination attempted')
        print('Streaming instance stopped normally')
        metadata_path.unlink(missing_ok=True)
    finally:
        os.close(descriptor)


class StreamingEngine:
    def __init__(self, transcriber, executor, publish, interval=1.0, minimum=1.2):
        self.transcriber, self.executor, self.publish = transcriber, executor, publish
        self.interval, self.minimum = interval, minimum
        self.controller = CaptureController(tail_seconds=.9)
        self.state = TranscriptState()
        self.stop = asyncio.Event()
        self.jobs = asyncio.Queue(maxsize=1)
        self.sequence = 0
        self.released_at = None
        self.next_preview_at = 0
        self.preview_disabled_generation = None
        self.paste_status = 'paste_shortcut_sent'

    def update(self):
        self.publish(self.state.value)

    def state_changed(self, value):
        now = time.monotonic()
        result = self.controller.on_state(value, now)
        LOG.info('receiver_state=%s capture=%s event=%s', value, self.controller.phase.value, result)
        if result == 'recording':
            self.state.begin(now)
            self.next_preview_at = now+self.minimum
        elif result == 'tail':
            self.released_at = now
            self.state.release()
        elif result in ('cancelled', 'overlap_cancelled'):
            self.state.cancel('录音已取消，请等待后重新按住说话')
        elif result == 'busy':
            notify('上一句正在处理，请松开并等待完成')
        self.update()

    def feed(self, pcm):
        result = self.controller.feed(pcm)
        if result == 'too_long':
            self.state.cancel('超过 60 秒，已取消；请松开后分段输入')
            self.update()

    async def infer(self, pcm):
        return await asyncio.get_running_loop().run_in_executor(self.executor, self.transcriber.transcribe, pcm)

    def processed_audio(self):
        return None

    async def previews(self):
        while not self.stop.is_set():
            await asyncio.sleep(.04)
            now = time.monotonic()
            if (self.controller.phase != CapturePhase.RECORDING
                    or self.preview_disabled_generation == self.state.generation
                    or self.controller.byte_count < self.minimum*32000 or now < self.next_preview_at):
                continue
            generation = self.state.generation
            pcm = b''.join(self.controller.parts)
            self.next_preview_at = now+self.interval
            try:
                text, metrics = await self.infer(pcm)
                if self.state.preview(generation, text, metrics, len(pcm)/32000):
                    LOG.info('preview session=%d audio=%.3f latency=%.3f chars=%d',
                             generation, len(pcm)/32000, metrics['transcription_seconds'], len(text))
                    self.update()
            except Exception:
                # A preview failure must not paste incomplete text or queue retries.
                LOG.exception('Preview failed; final full-audio recognition remains pending')
                self.preview_disabled_generation = generation

    async def tails(self):
        while not self.stop.is_set():
            await asyncio.sleep(.02)
            payload = self.controller.tick(time.monotonic())
            if payload is not None:
                self.sequence += 1
                self.state.finalizing()
                self.update()
                self.jobs.put_nowait((self.sequence, self.state.generation, payload, self.released_at))

    async def finals(self, directory, paste):
        while not self.stop.is_set():
            number, generation, pcm, released_at = await self.jobs.get()
            path = directory/f'{number:03d}.wav'
            write_pcm(path, pcm)
            record = {'session': number, 'generation': generation, 'status': 'started', 'audio_file': str(path),
                      'preview_events': list(self.state.events), 'first_preview_seconds': self.state.value.get('first_preview_seconds')}
            try:
                started = time.monotonic()
                text, metrics = await self.infer(pcm)
                if released_at is not None:
                    metrics['release_to_final_result_seconds'] = time.monotonic()-released_at
                if text and not self.stop.is_set() and generation == self.state.generation:
                    # Publish only the confirmed result, before clipboard/keyboard
                    # work. Capture stays busy until paste and cleanup finish.
                    self.state.finish(generation, text, 'final_ready', metrics)
                    self.update()
                processed = self.processed_audio()
                if processed is not None:
                    processed_path = directory/f'{number:03d}.processed.wav'
                    write_pcm(processed_path, processed)
                    record['processed_audio_file'] = str(processed_path)
                metrics['final_queue_and_inference_seconds'] = time.monotonic()-started
                record.update(metrics, text=text)
                if self.stop.is_set() or generation != self.state.generation:
                    record['status'] = 'cancelled_before_paste'
                elif not text:
                    record['status'] = 'no_speech'
                else:
                    (directory/f'{number:03d}.txt').write_text(text+'\n')
                    last_name = 'last-transcript.txt' if self.paste_status == 'paste_shortcut_sent' else 'last-replay-transcript.txt'
                    (RUNTIME/last_name).write_text(text+'\n')
                    await paste(text)
                    record['status'] = self.paste_status
                    key = 'release_to_paste_flow_seconds' if self.paste_status == 'paste_shortcut_sent' else 'release_to_replay_output_seconds'
                    record[key] = time.monotonic()-released_at
                self.state.finish(generation, text, record['status'], metrics)
                self.update()
                LOG.info('final session=%d status=%s asr=%.3f previews=%d', number,
                         record['status'], metrics['transcription_seconds'], len(record['preview_events']))
            except Exception as error:
                record.update(status='failed', error=str(error))
                self.state.value.update(phase='failed', message='转写或粘贴失败，请检查本地日志')
                self.update()
                LOG.exception('Final recognition/paste failed')
            finally:
                (directory/f'{number:03d}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')
                self.controller.complete()
                self.jobs.task_done()


class CloudStreamingEngine(StreamingEngine):
    """Share capture/paste rules, but never run snapshot or final re-recognition."""
    def __init__(self, config, publish, session_factory=None, *, audio_processing='off'):
        super().__init__(None, None, publish)
        self.controller.tail_seconds = .3
        from streaming_qwen import QwenRealtimeSession
        self.config = config
        self.session_factory = session_factory or (lambda: QwenRealtimeSession(config, audio_processing=audio_processing))
        self.cloud_session = None
        self.cloud_task = self.audio_queue = None
        self.cloud_tasks = set()
        self.state.value.update(model=config.model, backend='qwen-api',
                                message='正在准备云端识别（不加载本地模型）')

    def _abort(self, message):
        held = self.controller.phase == CapturePhase.RECORDING
        self.controller.complete()
        self.controller.ignored_hold = self.controller.ignored_hold or held
        self.state.cancel(message)
        self.state.value['phase'] = 'failed'
        self.update()

    def state_changed(self, value):
        generation = self.state.generation
        super().state_changed(value)
        if self.state.generation == generation:
            return
        if self.cloud_task and not self.cloud_task.done():
            self.cloud_task.cancel()
        if self.controller.phase != CapturePhase.RECORDING:
            return
        generation = self.state.generation
        # About five seconds at the receiver's 512-byte read size. A slow
        # connection cancels instead of silently dropping audio or growing RAM.
        self.audio_queue = asyncio.Queue(maxsize=320)
        session = self.session_factory()
        self.cloud_session = session

        def preview(text, seconds):
            if self.state.preview(generation, text, {}, seconds):
                self.update()

        self.cloud_task = asyncio.create_task(session.recognize(self.audio_queue, preview),
                                              name=f'qwen-cloud-{generation}')
        self.cloud_tasks.add(self.cloud_task)

        def done(task):
            self.cloud_tasks.discard(task)
            if task.cancelled():
                return
            error = task.exception()
            if (error and generation == self.state.generation
                    and self.controller.phase in (CapturePhase.RECORDING, CapturePhase.TAIL)):
                self._abort('云端识别失败，本句已取消；检查网络、地域、密钥和额度后重试')
                LOG.error('Cloud recognition failed during capture; no retry or paste')

        self.cloud_task.add_done_callback(done)

    def feed(self, pcm):
        active = self.controller.phase in (CapturePhase.RECORDING, CapturePhase.TAIL)
        generation = self.state.generation
        super().feed(pcm)
        if self.state.generation != generation:
            if self.cloud_task:
                self.cloud_task.cancel()
        elif active:
            try:
                self.audio_queue.put_nowait(pcm)
            except asyncio.QueueFull:
                self._abort('云端发送积压，本句已取消；请检查网络后重新按住说话')
                self.cloud_task.cancel()

    async def previews(self):
        # Previews come from server events, never from uploading PCM snapshots.
        await self.stop.wait()

    def processed_audio(self):
        return self.cloud_session.processed_audio if self.cloud_session else None

    async def infer(self, pcm):
        started = time.monotonic()
        # `pcm` stays on disk for the existing local receipt, never sent again.
        if not self.cloud_task.done():
            try:
                self.audio_queue.put_nowait(None)
            except asyncio.QueueFull:
                self.cloud_task.cancel()
                raise RuntimeError('Cloud send backlog; cancelled without retry') from None
        text, metrics = await self.cloud_task
        if metrics['uploaded_bytes'] != len(pcm):
            raise RuntimeError('Cloud upload length mismatch; refusing partial paste')
        metrics['realtime_session_seconds'] = metrics['transcription_seconds']
        metrics['transcription_seconds'] = time.monotonic()-started
        return text, metrics

    async def close(self):
        tasks = list(self.cloud_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class ReceiverSession:
    """Own only the receiver, capture, and keyboard started by this instance."""
    def __init__(self, directory):
        self.directory = directory
        self.keyboard = self.receiver = self.recorder = self.bus = self.lock = None
        self.files = []
        self.bytes = 0

    async def start(self, receiver_path, state_changed):
        from dbus_next.aio import MessageBus
        from dbus_next import Message, MessageType
        sources = subprocess.check_output(['pactl', 'list', 'sources', 'short'], text=True)
        if any('atvvoice-' in line for line in sources.splitlines()):
            raise RuntimeError('An ATVVoice source is already running; refusing duplicate receiver')
        if not os.access('/dev/uinput', os.W_OK):
            raise RuntimeError('/dev/uinput is unavailable; no permission changes attempted')
        run_dir = Path(os.environ['XDG_RUNTIME_DIR'])/'mi-remote-dictation'
        run_dir.mkdir(mode=0o700, exist_ok=True)
        if run_dir.is_symlink() or run_dir.stat().st_uid != os.getuid():
            raise RuntimeError('Unexpected runtime directory type/owner')
        run_dir.chmod(0o700)
        self.lock = (run_dir/'instance.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.bus = await MessageBus().connect()
        owns = await self.bus.call(Message(destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus', member='NameHasOwner', signature='s', body=[BUS_NAME]))
        if owns.message_type != MessageType.METHOD_RETURN or owns.body[0]:
            raise RuntimeError('Receiver D-Bus name is already owned')
        for name in ('receiver', 'keyboard', 'recorder'):
            self.files.append((self.directory/f'{name}.log').open('w'))
        receiver_log, keyboard_log, recorder_log = self.files
        socket = run_dir/'keyboard.sock'
        self.paste = DesktopPaste(self.bus, socket)
        await self.paste.connect()
        self.keyboard = await asyncio.create_subprocess_exec(str(LEGACY_RUNTIME/'tools/usr/bin/ydotoold'),
                    '-m', '--socket-path='+str(socket), '--socket-perm=0600', stdout=keyboard_log, stderr=keyboard_log)
        for _ in range(100):
            if self.keyboard.returncode is not None:
                raise RuntimeError('Keyboard daemon exited')
            if socket.exists():
                break
            await asyncio.sleep(.05)
        else:
            raise RuntimeError('Keyboard socket not ready')
        self.receiver = await asyncio.create_subprocess_exec(str(receiver_path.resolve()),
                '--device', 'C0:5D:39:C2:B1:79', '--name', 'rc003dictation', '--description', 'MI RC003-MS dictation',
                '--rc003-ms', '--prebuffer-ms', '220', '--gain', '0', '--frame-timeout', '0', '--keep-alive', '10', '-v',
                stdout=receiver_log, stderr=receiver_log)
        for _ in range(200):
            if self.receiver.returncode is not None:
                raise RuntimeError('Receiver exited; see receiver.log')
            try:
                xml = await self.bus.introspect(BUS_NAME, OBJECT_PATH)
                iface = self.bus.get_proxy_object(BUS_NAME, OBJECT_PATH, xml).get_interface('org.atvvoice.Daemon')
                if await iface.get_node_name() != SOURCE:
                    raise RuntimeError('Wrong receiver node')
                dump = await asyncio.create_subprocess_exec('pw-dump', stdout=asyncio.subprocess.PIPE)
                data, _ = await dump.communicate()
                if dump.returncode:
                    raise RuntimeError('pw-dump failed')
                target = receiver_target(json.loads(data))
                break
            except Exception:
                await asyncio.sleep(.1)
        else:
            raise RuntimeError('Receiver source not ready; wake remote and retry')
        iface.on_mic_state_changed(state_changed)
        self.recorder = await asyncio.create_subprocess_exec('pw-record', '--raw', '--target='+target,
                '--rate=16000', '--channels=1', '--format=s16', '--latency=256',
                '--properties=node.dont-fallback=true node.dont-move=true', '-',
                stdout=asyncio.subprocess.PIPE, stderr=recorder_log)
        first = await asyncio.wait_for(self.recorder.stdout.readexactly(512), 5)
        self.bytes += len(first)
        LOG.info('PCM_READY serial=%s', target)

    async def collect(self, engine):
        try:
            while not engine.stop.is_set():
                data = await self.recorder.stdout.readexactly(512)
                self.bytes += len(data)
                engine.feed(data)
        except asyncio.IncompleteReadError:
            LOG.error('Receiver audio ended; no fallback microphone')
            engine.state.cancel('音频连接中断，请检查终端')
            engine.update()
            engine.stop.set()

    async def close(self):
        await terminate(self.receiver, signal.SIGINT)
        await terminate(self.recorder, signal.SIGINT)
        await terminate(self.keyboard)
        if self.bus:
            self.bus.disconnect()
        for f in self.files:
            f.close()
        if self.lock:
            self.lock.close()
        (self.directory/'capture-health.json').write_text(json.dumps({'captured_seconds': self.bytes/32000,
            'recorder_returncode': self.recorder.returncode if self.recorder else None}, indent=2)+'\n')


async def run(args, server, executor):
    if args.backend == 'qwen-api':
        engine = CloudStreamingEngine(args.qwen_config, server.publish,
                                      audio_processing=args.audio_processing)
        engine.state.value.update(phase='ready',
            message='本地收音已就绪：按住后连接 Qwen 云端识别（会上传录音）')
        LOG.info('CLOUD_CONFIGURED model=%s region=%s; authentication happens on press',
                 args.qwen_config.model, args.qwen_config.region)
    else:
        from streaming_asr import QwenTranscriber
        start = time.monotonic()
        transcriber = await asyncio.get_running_loop().run_in_executor(executor, QwenTranscriber, args.model)
        LOG.info('MODEL_READY load_seconds=%.3f', time.monotonic()-start)
        engine = StreamingEngine(transcriber, executor, server.publish, args.preview_interval)
        await engine.infer(b'\x00\x00'*16000)  # Initialize CPU VAD without hallucinating silence.
        engine.state.value.update(phase='ready', backend='local', message='已就绪：点击输入框，按住语音键说话')
    directory = RUNTIME/'sessions'/time.strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True, exist_ok=False)
    receiver = ReceiverSession(directory)
    tasks = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, engine.stop.set)

    def critical_done(task):
        if not task.cancelled() and not engine.stop.is_set():
            LOG.error('Critical task stopped: %s %s', task.get_name(), task.exception())
            engine.stop.set()

    try:
        if args.replay:
            engine.paste_status = 'replay_output_saved'
            async def paste(text):
                (directory/'replay-input.txt').write_text(text+'\n')
        else:
            await receiver.start(args.receiver, engine.state_changed)
            await asyncio.sleep(.7)
            paste = receiver.paste.paste
            tasks.append(asyncio.create_task(receiver.collect(engine), name='audio-reader'))
        tasks += [asyncio.create_task(engine.previews(), name='previews'),
                  asyncio.create_task(engine.tails(), name='tails'),
                  asyncio.create_task(engine.finals(directory, paste), name='finals')]
        for task in tasks:
            task.add_done_callback(critical_done)
        engine.update()
        LOG.info('READY preview=%s session_dir=%s', server.url, directory)
        metadata_name = 'replay-current.json' if args.replay else 'current.json'
        (RUNTIME/metadata_name).write_text(json.dumps({'preview_url': server.url, 'session_dir': str(directory),
                                      'pid': os.getpid(), 'replay': bool(args.replay),
                                      'audio_processing': args.audio_processing,
                                      'tail_ms': round(engine.controller.tail_seconds * 1000),
                                      'backend': args.backend, 'model': engine.state.value['model']}, indent=2)+'\n')
        if args.replay:
            with wave.open(str(args.replay), 'rb') as w:
                assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
                pcm = w.readframes(w.getnframes())
            engine.state_changed('streaming')
            for i in range(0, len(pcm), 512):
                engine.feed(pcm[i:i+512])
                await asyncio.sleep(.016)
            engine.state_changed('connected')
            while engine.controller.phase != CapturePhase.IDLE:
                await asyncio.sleep(.05)
            await engine.jobs.join()
        else:
            notify('Qwen 已就绪：按住查看预览，松开后粘贴最终文字')
            await engine.stop.wait()
    finally:
        engine.stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(engine, CloudStreamingEngine):
            await engine.close()
        if not args.replay:
            await receiver.close()
        engine.state.value.update(phase='stopped', message='语音输入已停止')
        engine.update()
        LOG.info('STOPPED session_dir=%s', directory)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend', choices=('qwen-api', 'local'), default=os.environ.get('ASR_BACKEND', 'qwen-api'))
    p.add_argument('--qwen-region', choices=('beijing', 'singapore'), default='beijing')
    p.add_argument('--qwen-model', default='qwen3-asr-flash-realtime')
    p.add_argument('--audio-processing', choices=('off', 'light'), default='off',
                   help='Optional cloud PCM enhancement: light denoise + bounded AGC; default off')
    p.add_argument('--check-config', action='store_true', help='Validate configuration/dependencies without connecting or starting devices')
    p.add_argument('--model', type=Path, default=ROOT/'artifacts/asr-comparison/models/1.7B')
    p.add_argument('--receiver', type=Path, default=ROOT/'artifacts/2026-10-12-native-receiver/atvvoice-tested')
    p.add_argument('--port', type=int, default=18767)
    p.add_argument('--preview-interval', type=float, default=1.0)
    p.add_argument('--replay', type=Path)
    p.add_argument('--stop', action='store_true', help='Stop the recorded instance after checking its process identity')
    args = p.parse_args()
    if args.stop:
        stop_current()
        return
    if args.backend not in ('qwen-api', 'local'):
        p.error('backend must be qwen-api or local')
    if args.preview_interval < .5:
        p.error('preview interval must be at least 0.5 seconds')
    if args.audio_processing != 'off':
        if args.backend != 'qwen-api':
            p.error('audio processing currently supports qwen-api only')
        try:
            from audio_enhancement import SpeechEnhancer
        except ImportError:
            p.error('请先安装 audio-enhancement-requirements.txt 中的依赖')
    if args.backend == 'qwen-api':
        from streaming_qwen import QwenConfig
        try:
            args.qwen_config = QwenConfig.from_env(args.qwen_region, args.qwen_model)
            from websockets.asyncio.client import connect  # Cloud dependency only.
        except (ValueError, ImportError) as error:
            p.error(str(error) if isinstance(error, ValueError) else '请先安装 cloud-requirements.txt 中的依赖')
    if args.check_config:
        print(f'Configuration valid: backend={args.backend}; audio_processing={args.audio_processing}; no cloud connection or device started')
        return
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
    RUNTIME.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    server = PreviewServer(args.port)
    server.start()
    LOG.info('PREVIEW %s', server.url)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='qwen') as executor:
            asyncio.run(run(args, server, executor))
    finally:
        server.close()


if __name__ == '__main__':
    main()
