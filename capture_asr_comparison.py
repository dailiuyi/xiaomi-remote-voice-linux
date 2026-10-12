"""Record test WAVs from the existing receiver; never paste or own Bluetooth."""
import asyncio
import json
from pathlib import Path
import signal
import time

from dictation import BUS_NAME, OBJECT_PATH, receiver_target, write_pcm, terminate
from dictation_core import CaptureController

ROOT = Path(__file__).resolve().parent / 'artifacts/asr-comparison'


async def main():
    from dbus_next.aio import MessageBus
    bus = await MessageBus().connect()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    directory = ROOT / 'recordings' / time.strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True)
    log = (directory / 'recorder.log').open('w')
    state_log = []
    controller = CaptureController(tail_seconds=.9)
    xml = await bus.introspect(BUS_NAME, OBJECT_PATH)
    iface = bus.get_proxy_object(BUS_NAME, OBJECT_PATH, xml).get_interface('org.atvvoice.Daemon')
    def changed(state):
        event = controller.on_state(state, time.monotonic())
        state_log.append({'state': state, 'event': event, 'at': time.time()})
        print('STATE', state, event, flush=True)
    iface.on_mic_state_changed(changed)
    dump = await asyncio.create_subprocess_exec('pw-dump', stdout=asyncio.subprocess.PIPE)
    output, _ = await dump.communicate()
    serial = receiver_target(json.loads(output))
    recorder = await asyncio.create_subprocess_exec(
        'pw-record', '--raw', '--target=' + serial, '--rate=16000', '--channels=1',
        '--format=s16', '--latency=256',
        '--properties=node.dont-fallback=true node.dont-move=true', '-',
        stdout=asyncio.subprocess.PIPE, stderr=log)
    completed = []
    async def read():
        try:
            while not stop.is_set():
                controller.feed(await recorder.stdout.readexactly(512))
        except asyncio.IncompleteReadError:
            print('ERROR receiver PCM ended', flush=True)
            stop.set()
    async def tick():
        while not stop.is_set():
            await asyncio.sleep(.02)
            pcm = controller.tick(time.monotonic())
            if pcm is not None:
                path = directory / f'{len(completed)+1:03d}.wav'
                write_pcm(path, pcm)
                completed.append({'path': str(path), 'audio_seconds': len(pcm) / 32000})
                print('SAVED', completed[-1], flush=True)
                controller.complete()
                if len(completed) >= 4:
                    stop.set()
    tasks = []
    try:
        await asyncio.wait_for(recorder.stdout.readexactly(512), 5)
        tasks = [asyncio.create_task(read()), asyncio.create_task(tick())]
        print('READY', directory, 'existing source serial', serial, flush=True)
        await asyncio.wait_for(stop.wait(), 600)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await terminate(recorder, signal.SIGINT)
        bus.disconnect()
        log.close()
        (directory / 'capture.json').write_text(json.dumps({'source_serial': serial,
            'recordings': completed, 'state_events': state_log}, ensure_ascii=False, indent=2)+'\n')
        print('DONE', len(completed), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
