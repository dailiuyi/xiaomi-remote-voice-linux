import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import os
import unittest
from unittest.mock import AsyncMock
import urllib.request
import urllib.error

from streaming_core import TranscriptState
from streaming_dictation import StreamingEngine, stop_current
from streaming_preview import PreviewServer


class StateTests(unittest.TestCase):
    def test_stop_refuses_a_reused_or_unrelated_recorded_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'current.json'
            path.write_text(json.dumps({'pid':os.getpid()}))
            with self.assertRaises(RuntimeError):
                stop_current(path)

    def test_released_preview_and_previous_session_results_are_discarded(self):
        s = TranscriptState()
        first = s.begin(10)
        self.assertTrue(s.preview(first, '临时文字', {}, 1.2, 11.5))
        s.release()
        self.assertFalse(s.preview(first, '过时预览', {}, 2.2, 12.5))
        s.finalizing()
        s.finish(first, '最终文字', 'paste_shortcut_sent', {})
        self.assertEqual(s.value['final'], '最终文字')
        s.begin(20)
        self.assertFalse(s.finish(first, '旧句结果', 'paste_shortcut_sent', {}))
        self.assertEqual(s.value['final'], '')

    def test_cancel_and_silence_cannot_show_preview(self):
        s = TranscriptState()
        first = s.begin(10)
        self.assertFalse(s.preview(first, '', {}, 1.2, 12))
        s.cancel('断开')
        self.assertFalse(s.preview(first, '迟到结果', {}, 1.2, 12))
        self.assertEqual(s.events, [])


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inflight_preview_is_discarded_and_only_final_is_pasted(self):
        entered, release = threading.Event(), threading.Event()
        class Model:
            calls = 0
            def transcribe(self, pcm):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    release.wait(2)
                    return '旧预览', {'transcription_seconds': .1}
                return '完整最终文字', {'transcription_seconds': .1}
        with ThreadPoolExecutor(max_workers=1) as pool, tempfile.TemporaryDirectory() as d:
            state_updates = []
            e = StreamingEngine(Model(), pool, lambda value: state_updates.append(dict(value)), minimum=.01)
            e.controller.tail_seconds=0
            e.state_changed('streaming')
            e.feed(b'\x00\x00'*1600)
            previews=asyncio.create_task(e.previews())
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set())
            e.state_changed('connected')
            tails=asyncio.create_task(e.tails())
            paste=AsyncMock()
            finals=asyncio.create_task(e.finals(Path(d), paste))
            await asyncio.sleep(.05)
            release.set()
            for _ in range(100):
                if paste.await_count: break
                await asyncio.sleep(.01)
            await e.jobs.join()
            self.assertEqual(paste.await_args.args, ('完整最终文字',))
            self.assertEqual(paste.await_count, 1)
            self.assertFalse(any(s['preview']=='旧预览' for s in state_updates))
            e.stop.set()
            for t in (previews,tails,finals):t.cancel()
            await asyncio.gather(previews,tails,finals, return_exceptions=True)

    async def test_final_silence_never_touches_input(self):
        class Model:
            def transcribe(self, pcm):return '', {'transcription_seconds': .01}
        with ThreadPoolExecutor(max_workers=1) as pool, tempfile.TemporaryDirectory() as d:
            e=StreamingEngine(Model(),pool,lambda _:None)
            e.state_changed('streaming'); e.feed(b'\x00\x00'*1600)
            e.state_changed('connected'); e.controller.tick(float('inf'))
            e.state.finalizing(); e.jobs.put_nowait((1,e.state.generation,b'\x00\x00'*1600,0))
            paste=AsyncMock(); final=asyncio.create_task(e.finals(Path(d),paste))
            await e.jobs.join()
            paste.assert_not_awaited()
            self.assertEqual(e.state.value['phase'],'no_speech')
            e.stop.set(); final.cancel(); await asyncio.gather(final,return_exceptions=True)


class PreviewTests(unittest.TestCase):
    def test_local_panel_requires_private_path_and_disallows_foreign_origin(self):
        s=PreviewServer(0); s.start()
        try:
            s.publish({'preview':'只在本地展示'})
            with urllib.request.urlopen(s.url+'status') as response:
                self.assertEqual(json.load(response)['preview'],'只在本地展示')
                self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(urllib.request.Request(s.url+'status',headers={'Origin':'https://example.com'}))
            self.assertEqual(err.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(s.url.replace(s.token,'invalid')+'status')
        finally:s.close()


if __name__=='__main__':unittest.main()
