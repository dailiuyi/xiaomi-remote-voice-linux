#!/usr/bin/env python3
"""RC003-MS: local transcription followed by clipboard paste into current focus."""
import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import time
import wave

from dictation_core import CaptureController

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'artifacts/dictation-runtime'
BUS_NAME = 'org.atvvoice.rc003dictation'
OBJECT_PATH = '/org/atvvoice/Daemon'
SOURCE = 'atvvoice-rc003dictation'
LOG = logging.getLogger('dictation')


def receiver_target(objects):
    matches = []
    for obj in objects:
        props = (obj.get('info') or {}).get('props') or {}
        if (obj.get('type') == 'PipeWire:Interface:Node'
                and props.get('node.name') == SOURCE
                and props.get('media.class') == 'Audio/Source'):
            matches.append(props.get('object.serial'))
    if len(matches) != 1 or not str(matches[0]).isdigit():
        raise RuntimeError('Expected one receiver source with a stable object.serial')
    return str(matches[0])


def notify(message):
    # Do not expose transcript text through desktop notifications.
    subprocess.Popen(['notify-send', '-a', 'MI 语音输入', '-t', '2500',
                      'MI 语音输入', message], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


class LocalTranscriber:
    def __init__(self, model_path, device):
        from faster_whisper import WhisperModel
        from opencc import OpenCC
        self.model = WhisperModel(str(model_path), device=device,
                                 compute_type='int8_float16' if device == 'cuda' else 'int8',
                                 local_files_only=True, cpu_threads=8)
        self.convert = OpenCC('t2s').convert

    def transcribe(self, audio):
        if isinstance(audio, (str, Path)):
            # Our receiver produces 16kHz mono PCM. Read it directly instead of
            # relying on PyAV's version-dependent compressed-audio interface.
            import numpy as np
            with wave.open(str(audio), 'rb') as stream:
                if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
                    raise ValueError('Expected 16000Hz mono s16le WAV')
                audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768
        start = time.monotonic()
        segments, info = self.model.transcribe(
            audio, language='zh', beam_size=5, vad_filter=True,
            vad_parameters={'min_silence_duration_ms': 400, 'speech_pad_ms': 200},
            condition_on_previous_text=False, temperature=0,
            initial_prompt='中文软件开发口述，可能包含英文名称和编程术语。')
        text = self.convert(''.join(s.text for s in segments).strip())
        return text, {'transcription_seconds': time.monotonic() - start,
                      'audio_seconds': info.duration,
                      'speech_seconds_after_vad': info.duration_after_vad,
                      'model': 'Systran/faster-whisper-medium', 'device': self.model.model.device}


class DesktopPaste:
    def __init__(self, bus, socket):
        self.bus, self.socket = bus, socket
        self.clipboard = None

    async def connect(self):
        xml = await self.bus.introspect('org.kde.klipper', '/klipper')
        obj = self.bus.get_proxy_object('org.kde.klipper', '/klipper', xml)
        self.clipboard = obj.get_interface('org.kde.klipper.klipper')

    async def paste(self, text):
        if not text.strip() or '\x00' in text:
            raise ValueError('Empty/invalid transcript')
        previous = await self.clipboard.call_get_clipboard_contents()
        await self.clipboard.call_set_clipboard_contents(text)
        env = dict(os.environ, YDOTOOL_SOCKET=str(self.socket))
        command = str(RUNTIME / 'tools/usr/bin/ydotool')
        proc = None
        try:
            await asyncio.sleep(.08)
            proc = await asyncio.create_subprocess_exec(
                command, 'key', '--key-delay', '30', '29:1', '47:1', '47:0', '29:0',
                env=env, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            _, error = await asyncio.wait_for(proc.communicate(), 3)
            if proc.returncode:
                raise RuntimeError(error.decode(errors='replace').strip())
            # Allow asynchronous applications to consume the selection.
            await asyncio.sleep(.4)
        except BaseException:
            if proc is not None and proc.returncode is None:
                await terminate(proc)
            # A failed/cancelled backend must not leave virtual Ctrl held down.
            try:
                release = await asyncio.create_subprocess_exec(
                    command, 'key', '47:0', '29:0', env=env,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(release.wait(), 1)
            except Exception:
                LOG.error('Could not confirm virtual modifier release')
            raise
        finally:
            # Do not overwrite a clipboard value the user changed meanwhile.
            if await self.clipboard.call_get_clipboard_contents() == text:
                await self.clipboard.call_set_clipboard_contents(previous)


def write_pcm(path, payload):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(payload)


async def terminate(proc, sig=signal.SIGTERM):
    if proc is None or proc.returncode is not None:
        return
    proc.send_signal(sig)
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        LOG.error('Child did not stop before deadline; terminating PID %d', proc.pid)
        proc.kill()
        await proc.wait()


async def live(args, transcriber):
    import numpy as np
    from dbus_next.aio import MessageBus
    from dbus_next import Message, MessageType
    sources = subprocess.check_output(['pactl', 'list', 'sources', 'short'], text=True)
    if any('atvvoice-' in line for line in sources.splitlines()):
        raise RuntimeError('An ATVVoice source is already running; leave it unchanged and stop this duplicate start')
    if not os.access('/dev/uinput', os.W_OK):
        raise RuntimeError('Current user cannot write /dev/uinput; no permission changes were attempted')
    run_dir = Path(os.environ['XDG_RUNTIME_DIR']) / 'mi-remote-dictation'
    run_dir.mkdir(mode=0o700, exist_ok=True)
    if run_dir.is_symlink() or run_dir.stat().st_uid != os.getuid():
        raise RuntimeError('Unexpected runtime directory owner/type')
    run_dir.chmod(0o700)
    socket = run_dir / 'keyboard.sock'
    # A private flock protects the receiver and keyboard from duplicate starts.
    lock = (run_dir / 'instance.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    bus = await MessageBus().connect()
    owns = await bus.call(Message(destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
                                 interface='org.freedesktop.DBus', member='NameHasOwner',
                                 signature='s', body=[BUS_NAME]))
    if owns.message_type != MessageType.METHOD_RETURN or owns.body[0]:
        raise RuntimeError('Receiver D-Bus name unavailable/already owned')
    directory = RUNTIME / 'sessions' / time.strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True, exist_ok=False)
    keyboard = receiver = recorder = None
    worker = reader = ticker = None
    receiver_log = (directory / 'receiver.log').open('w')
    keyboard_log = (directory / 'keyboard.log').open('w')
    recorder_log = (directory / 'recorder.log').open('w')
    controller = CaptureController(tail_seconds=args.tail_ms / 1000)
    paste = DesktopPaste(bus, socket)
    jobs = asyncio.Queue(maxsize=1)
    sequence = 0
    captured_bytes = 0

    def task_finished(task):
        if task.cancelled() or stop.is_set():
            return
        error = task.exception()
        LOG.error('Critical task %s stopped: %s', task.get_name(), error or 'unexpected return')
        stop.set()

    def state_changed(state):
        result = controller.on_state(state, time.monotonic())
        LOG.info('receiver_state=%s capture=%s event=%s', state, controller.phase.value, result)
        if result == 'recording':
            notify('正在收音，松开语音键后开始转写')
        elif result == 'tail':
            notify('收音结束，正在保留尾音并准备转写')
        elif result in ('busy', 'overlap_cancelled'):
            notify('正在处理上一句，请松开按键，等待完成后再说；过快连续按键已取消以免混音')
        elif result == 'cancelled':
            notify('遥控器断开，本次未完成录音已取消')

    async def collect():
        nonlocal captured_bytes
        try:
            while not stop.is_set():
                payload = await recorder.stdout.readexactly(512)
                captured_bytes += len(payload)
                if controller.feed(payload) == 'too_long':
                    notify('本次收音超过 60 秒，已取消；请松开后分段输入')
        except asyncio.IncompleteReadError:
            LOG.error('Audio source ended; stopping without fallback to system microphone')
            stop.set()

    async def finish_tail():
        nonlocal sequence
        while not stop.is_set():
            await asyncio.sleep(.02)
            payload = controller.tick(time.monotonic())
            if payload is not None:
                sequence += 1
                jobs.put_nowait((sequence, payload, time.monotonic()))

    async def transcribe_jobs():
        while not stop.is_set():
            number, payload, released_tail = await jobs.get()
            wav_path = directory / f'{number:03d}.wav'
            write_pcm(wav_path, payload)
            notify('正在本地转写，请保持目标输入框获得焦点')
            record = {'session': number, 'status': 'started', 'audio_file': str(wav_path)}
            try:
                audio = np.frombuffer(payload, dtype='<i2').astype(np.float32) / 32768
                text, metrics = await asyncio.to_thread(transcriber.transcribe, audio)
                record.update(metrics)
                record['text'] = text
                if stop.is_set():
                    record['status'] = 'cancelled_before_paste'
                elif not text:
                    record['status'] = 'no_speech'
                    notify('没有识别到有效语音，本次未输入文字')
                else:
                    (directory / f'{number:03d}.txt').write_text(text + '\n')
                    (RUNTIME / 'last-transcript.txt').write_text(text + '\n')
                    await paste.paste(text)
                    record['status'] = 'paste_shortcut_sent'
                    record['tail_complete_to_paste_seconds'] = time.monotonic() - released_tail
                    notify('转写完成，已粘贴；可继续按住说下一句')
                    LOG.info('session=%d paste_shortcut_sent chars=%d transcription=%.3fs',
                             number, len(text), metrics['transcription_seconds'])
            except Exception as error:
                record['status'] = 'failed'
                record['error'] = str(error)
                LOG.exception('Transcription/input failed')
                notify('转写或粘贴失败，详情见本地日志；已有文字保存在 last-transcript.txt')
            finally:
                (directory / f'{number:03d}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')
                controller.complete()
                jobs.task_done()

    try:
        await paste.connect()
        keyboard = await asyncio.create_subprocess_exec(
            str(RUNTIME / 'tools/usr/bin/ydotoold'), '-m', '--socket-path=' + str(socket),
            '--socket-perm=0600', stdout=keyboard_log, stderr=keyboard_log)
        for _ in range(100):
            if keyboard.returncode is not None:
                raise RuntimeError('Keyboard daemon exited; see keyboard.log')
            if socket.exists():
                break
            await asyncio.sleep(.05)
        else:
            raise RuntimeError('Keyboard daemon did not become ready')
        receiver = await asyncio.create_subprocess_exec(
            str(args.receiver.resolve()), '--device', 'C0:5D:39:C2:B1:79',
            '--name', 'rc003dictation', '--description', 'MI RC003-MS dictation',
            '--rc003-ms', '--prebuffer-ms', '220', '--gain', '0',
            '--frame-timeout', '0', '--keep-alive', '10', '-v',
            stdout=receiver_log, stderr=receiver_log)
        for _ in range(200):
            if receiver.returncode is not None:
                raise RuntimeError('Receiver exited; see receiver.log')
            try:
                xml = await bus.introspect(BUS_NAME, OBJECT_PATH)
                proxy = bus.get_proxy_object(BUS_NAME, OBJECT_PATH, xml)
                iface = proxy.get_interface('org.atvvoice.Daemon')
                node = await iface.get_node_name()
                if node != SOURCE:
                    raise RuntimeError('Unexpected receiver node name')
                sources = await asyncio.create_subprocess_exec('pactl', 'list', 'sources', 'short', stdout=asyncio.subprocess.PIPE)
                output, _ = await sources.communicate()
                if SOURCE.encode() in output:
                    break
            except Exception:
                pass
            await asyncio.sleep(.1)
        else:
            raise RuntimeError('Receiver/source not ready before deadline')
        iface.on_mic_state_changed(state_changed)
        dump = await asyncio.create_subprocess_exec('pw-dump', stdout=asyncio.subprocess.PIPE)
        output, _ = await dump.communicate()
        if dump.returncode:
            raise RuntimeError('Could not inspect receiver PipeWire source')
        target = receiver_target(json.loads(output))
        recorder = await asyncio.create_subprocess_exec(
            'pw-record', '--raw', '--target=' + target, '--rate=16000', '--channels=1',
            '--format=s16', '--latency=256',
            '--properties=node.dont-fallback=true node.dont-move=true', '-',
            stdout=asyncio.subprocess.PIPE, stderr=recorder_log)
        first = await asyncio.wait_for(recorder.stdout.readexactly(512), 5)
        captured_bytes += len(first)
        controller.feed(first)
        reader = asyncio.create_task(collect(), name='audio-reader')
        ticker = asyncio.create_task(finish_tail(), name='capture-timer')
        worker = asyncio.create_task(transcribe_jobs(), name='transcription-worker')
        for task in (reader, ticker, worker):
            task.add_done_callback(task_finished)
        # Model loaded and silence/VAD check completed before ready.
        await asyncio.sleep(.7)  # Give desktop time to discover virtual keyboard.
        if recorder.returncode is not None:
            raise RuntimeError('Audio capture failed to start')
        LOG.info('READY source=%s serial=%s first_pcm_bytes=%d session_dir=%s',
                 SOURCE, target, captured_bytes, directory)
        notify('已就绪：点击输入框，按住语音键说话，松开后等待粘贴')
        await stop.wait()
    finally:
        stop.set()
        for task in (reader, ticker, worker):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (reader, ticker, worker) if t), return_exceptions=True)
        # Keep capture connected while the owned receiver drains its tail.
        await terminate(receiver, signal.SIGINT)
        await terminate(recorder, signal.SIGINT)
        await terminate(keyboard)
        bus.disconnect()
        for file in (receiver_log, recorder_log, keyboard_log):
            file.close()
        (directory / 'capture-health.json').write_text(json.dumps({
            'captured_bytes': captured_bytes,
            'capture_seconds': captured_bytes / 32000,
            'capture_returncode': recorder.returncode if recorder else None,
        }, indent=2) + '\n')
        lock.close()
        LOG.info('Stopped owned receiver, capture and keyboard; no defaults changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['transcribe', 'live'])
    parser.add_argument('files', nargs='*', type=Path)
    parser.add_argument('--model', type=Path, default=RUNTIME / 'models/medium')
    parser.add_argument('--device', choices=['cuda', 'cpu'], default='cuda')
    parser.add_argument('--tail-ms', type=int, default=900, choices=range(250, 1001), metavar='250..1000')
    parser.add_argument('--receiver', type=Path, default=ROOT / 'artifacts/2026-10-12-native-receiver/atvvoice-tested')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    LOG.info('Loading local medium model on %s', args.device)
    transcriber = LocalTranscriber(args.model, args.device)
    if args.command == 'transcribe':
        records = []
        for path in args.files:
            text, metrics = transcriber.transcribe(str(path))
            records.append({'file': str(path), 'text': text, **metrics})
        output = json.dumps(records, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(output + '\n')
        print(output)
    else:
        import numpy as np
        transcriber.transcribe(np.zeros(16000, dtype=np.float32))
        asyncio.run(live(args, transcriber))


if __name__ == '__main__':
    main()
