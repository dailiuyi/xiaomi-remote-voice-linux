"""Checks for the offline diagnostic decoder and control-message replay."""
import json
import tempfile
import unittest
from pathlib import Path

from analyze_atvv_packets import Decoder, postprocess, replay


class PacketReplayTests(unittest.TestCase):
    def test_known_nibbles_are_decoded_high_first(self):
        decoder = Decoder()
        self.assertEqual(decoder.decode(bytes.fromhex("7f")), [11, -19])
        self.assertEqual(decoder.index, 16)

    def test_state_bounds_and_explicit_reset(self):
        decoder = Decoder()
        samples = decoder.decode(b"\x77" * 120)
        self.assertEqual(max(samples), 32767)
        self.assertEqual(decoder.index, 88)
        decoder.reset()
        self.assertEqual(decoder.decode(b"\x00" * 2), [0, 0, 0, 0])

    def test_postprocessing_preserves_constant_and_removes_isolated_spike(self):
        self.assertEqual(postprocess([-1234] * 240), [-1234] * 240)
        samples = [0] * 240
        samples[100] = 30000
        self.assertEqual(postprocess(samples), [0] * 240)

    def test_new_session_policy_and_audio_sync(self):
        entries = [
            ("ctl", "04030201"), ("rx", "77" * 120), ("ctl", "0002"),
            ("ctl", "04030202"), ("rx", "00" * 120), ("ctl", "0002"),
            ("ctl", "04030203"), ("ctl", "0a02000003e800"),
            ("rx", "00" * 120), ("ctl", "0002"),
            ("end", ""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.jsonl"
            path.write_text("".join(json.dumps({"t_ns": i * 15000000, "kind": kind, "hex": data}) + "\n"
                                    for i, (kind, data) in enumerate(entries)))
            carry, reset = replay([path], False), replay([path], True)
        self.assertNotEqual(carry[1]["samples"], reset[1]["samples"])
        self.assertEqual(reset[1]["samples"], [0] * 240)
        for result in (carry, reset):
            self.assertEqual(result[2]["samples"], [1000] * 240)
            self.assertEqual(result[2]["sync_count"], 1)


if __name__ == "__main__":
    unittest.main()
