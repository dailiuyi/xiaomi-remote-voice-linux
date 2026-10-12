import unittest
from unittest.mock import AsyncMock, patch

from dictation_core import CaptureController, CapturePhase
from dictation import DesktopPaste, receiver_target


class SourceTests(unittest.TestCase):
    def test_target_is_unique_audio_source_serial_not_reusable_node_id(self):
        objects = [{'id': 109, 'type': 'PipeWire:Interface:Node', 'info': {'props': {
            'node.name': 'atvvoice-rc003dictation', 'media.class': 'Audio/Source',
            'object.serial': 405}}}]
        self.assertEqual(receiver_target(objects), '405')
        objects[0]['info']['props']['media.class'] = 'Stream/Input/Audio'
        with self.assertRaises(RuntimeError):
            receiver_target(objects)

    def test_missing_or_duplicate_source_never_selects_default_microphone(self):
        with self.assertRaises(RuntimeError):
            receiver_target([])
        node = {'type': 'PipeWire:Interface:Node', 'info': {'props': {
            'node.name': 'atvvoice-rc003dictation', 'media.class': 'Audio/Source',
            'object.serial': 405}}}
        with self.assertRaises(RuntimeError):
            receiver_target([node, node])


class CaptureTests(unittest.TestCase):
    def test_release_keeps_bounded_output_tail_before_transcribing(self):
        c = CaptureController(tail_seconds=0.4)
        c.on_state('streaming', 10.0)
        c.feed(b'\x01\x00' * 256)
        c.on_state('connected', 10.1)
        c.feed(b'\x02\x00' * 256)
        self.assertIsNone(c.tick(10.49))
        self.assertEqual(c.tick(10.51), b'\x01\x00' * 256 + b'\x02\x00' * 256)
        self.assertEqual(c.phase, CapturePhase.TRANSCRIBING)
        self.assertIsNone(c.tick(11))

    def test_busy_press_is_rejected_until_its_own_release(self):
        c = CaptureController()
        c.on_state('streaming', 1)
        c.feed(b'\x01\x00' * 8000)
        c.on_state('connected', 2)
        c.tick(3)
        self.assertEqual(c.on_state('streaming', 4), 'busy')
        c.complete()
        self.assertEqual(c.on_state('streaming', 5), 'busy')
        c.on_state('connected', 6)
        self.assertEqual(c.on_state('streaming', 7), 'recording')

    def test_rapid_second_press_cannot_mix_two_utterances(self):
        c = CaptureController()
        c.on_state('streaming', 1)
        c.feed(b'\x01\x00' * 256)
        c.on_state('connected', 2)
        self.assertEqual(c.on_state('streaming', 2.1), 'overlap_cancelled')
        c.feed(b'\x02\x00' * 256)
        self.assertIsNone(c.tick(3))
        c.on_state('connected', 4)
        self.assertEqual(c.on_state('streaming', 5), 'recording')

    def test_disconnect_discards_incomplete_utterance(self):
        c = CaptureController()
        c.on_state('streaming', 1)
        c.feed(b'\x01\x00' * 256)
        self.assertEqual(c.on_state('disconnected', 2), 'cancelled')
        self.assertIsNone(c.tick(3))
        self.assertEqual(c.phase, CapturePhase.IDLE)

    def test_duration_limit_cancels_instead_of_transcribing_partial_audio(self):
        c = CaptureController(max_seconds=1)
        c.on_state('streaming', 1)
        self.assertEqual(c.feed(b'\x01\x00' * 16001), 'too_long')
        c.on_state('connected', 3)
        self.assertIsNone(c.tick(4))
        self.assertEqual(c.on_state('streaming', 5), 'recording')

    def test_idle_audio_is_not_captured_and_duplicate_states_are_safe(self):
        c = CaptureController()
        c.feed(b'\x01\x00' * 256)
        c.on_state('connected', 1)
        c.on_state('streaming', 2)
        c.feed(b'\x02\x00' * 256)
        c.on_state('streaming', 2.1)
        c.on_state('connected', 3)
        c.on_state('connected', 3.1)
        self.assertEqual(c.tick(4), b'\x02\x00' * 256)


class Clipboard:
    def __init__(self):
        self.value = 'existing clipboard'

    async def call_get_clipboard_contents(self):
        return self.value

    async def call_set_clipboard_contents(self, text):
        self.value = text


class PasteTests(unittest.IsolatedAsyncioTestCase):
    async def test_paste_restores_clipboard_and_sends_only_ctrl_v(self):
        paste = DesktopPaste(None, '/private/test.sock')
        paste.clipboard = Clipboard()
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b'', b'')
        with patch('dictation.asyncio.create_subprocess_exec', return_value=process) as create:
            await paste.paste('中文 code')
        self.assertEqual(paste.clipboard.value, 'existing clipboard')
        keys = create.call_args.args[4:]
        self.assertEqual(keys, ('29:1', '47:1', '47:0', '29:0'))

    async def test_failed_shortcut_restores_clipboard(self):
        paste = DesktopPaste(None, '/private/test.sock')
        paste.clipboard = Clipboard()
        process = AsyncMock()
        process.returncode = 1
        process.communicate.return_value = (b'', b'backend unavailable')
        with patch('dictation.asyncio.create_subprocess_exec', return_value=process):
            with self.assertRaises(RuntimeError):
                await paste.paste('中文')
        self.assertEqual(paste.clipboard.value, 'existing clipboard')

    async def test_user_clipboard_change_during_paste_is_preserved(self):
        paste = DesktopPaste(None, '/private/test.sock')
        paste.clipboard = Clipboard()
        async def change_clipboard():
            paste.clipboard.value = 'user copied something else'
            return b'', b''
        process = AsyncMock()
        process.returncode = 0
        process.communicate.side_effect = change_clipboard
        with patch('dictation.asyncio.create_subprocess_exec', return_value=process):
            await paste.paste('中文')
        self.assertEqual(paste.clipboard.value, 'user copied something else')

    async def test_empty_transcript_never_touches_clipboard_or_keyboard(self):
        paste = DesktopPaste(None, '/private/test.sock')
        paste.clipboard = Clipboard()
        with patch('dictation.asyncio.create_subprocess_exec') as create:
            with self.assertRaises(ValueError):
                await paste.paste('  ')
            create.assert_not_called()
        self.assertEqual(paste.clipboard.value, 'existing clipboard')


if __name__ == '__main__':
    unittest.main()
