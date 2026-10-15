"""Signal invariants and synthetic regression checks, not ASR acceptance."""
import unittest

import numpy as np

from audio_enhancement import CEILING, RATE, SpeechEnhancer


def encode(samples):
    return np.rint(np.clip(samples, -.999, .999) * 32768).astype('<i2').tobytes()


def process(pcm, chunk=512, **kwargs):
    processor = SpeechEnhancer(**kwargs)
    output = b''.join(processor.feed(pcm[i:i+chunk]) for i in range(0, len(pcm), chunk))
    output += processor.finish()
    return np.frombuffer(output, dtype='<i2').astype(float) / 32768, processor


def rms(samples):
    return np.sqrt(np.mean(samples ** 2))


class EnhancementTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(163)

    def test_empty_short_and_partial_tail_preserve_length_and_alignment(self):
        for size in (0, 1, 159, 160, 161, 319, 320, 321, 1573):
            with self.subTest(size=size):
                samples = self.rng.normal(0, .01, size)
                pcm = encode(samples)
                output, processor = process(pcm)
                # No gain or denoising before warmup; proves WOLA onset/tail
                # compensation rather than merely testing the output length.
                np.testing.assert_array_equal(encode(output), pcm)
                self.assertEqual(processor.finish(), b'')
                self.assertEqual(processor.output_samples, size)
                with self.assertRaises(ValueError):
                    processor.feed(b'\0\0')

    def test_chunk_boundaries_do_not_change_output(self):
        pcm = encode(self.rng.normal(0, .004, RATE * 2 + 137))
        expected, _ = process(pcm)
        for chunk in (2, 318, 320, 514, 3200, len(pcm)):
            output, _ = process(pcm, chunk)
            np.testing.assert_array_equal(output, expected)

    def test_zero_padding_is_not_noise_calibration_and_stays_silent(self):
        output, processor = process(bytes(RATE * 6))
        self.assertEqual(np.count_nonzero(output), 0)
        self.assertEqual(processor.frames, 0)
        self.assertEqual(processor.max_gain_db, 0)

    def test_stationary_quiet_noise_is_reduced_and_not_amplified(self):
        samples = self.rng.normal(0, .0008, RATE * 3)
        output, processor = process(encode(samples))
        reduction = 20 * np.log10(rms(samples[RATE:]) / rms(output[RATE:]))
        self.assertGreater(reduction, .5)
        self.assertLess(reduction, 6.2)
        self.assertLess(processor.max_gain_db, .01)
        self.assertGreater(rms(output), 0)

    def test_soft_modulated_voice_is_lifted_without_flattening_consonants(self):
        t = np.arange(RATE * 4) / RATE
        envelope = np.maximum(0, np.sin(2 * np.pi * 2 * t)) ** 2
        voice = .012 * envelope * (np.sin(2 * np.pi * 230 * t) + .25 * np.sin(2 * np.pi * 1700 * t))
        samples = voice + self.rng.normal(0, .0005, len(t))
        output, processor = process(encode(samples))
        active = (envelope > .5) & (t > 1)
        self.assertGreater(rms(output[active]) / rms(samples[active]), 1.15)
        self.assertGreater(np.corrcoef(output[active], voice[active])[0, 1], .93)
        self.assertLessEqual(processor.max_gain_db, 12)

    def test_loud_click_after_soft_voice_is_bounded_and_recovers(self):
        t = np.arange(RATE * 3) / RATE
        voice = .01 * np.maximum(0, np.sin(2 * np.pi * 2 * t)) ** 2 * np.sin(2 * np.pi * 400 * t)
        samples = voice + self.rng.normal(0, .0004, len(t))
        samples[RATE+2000:RATE+2010] = .99
        samples[2*RATE:] = 0
        output, processor = process(encode(samples))
        self.assertLessEqual(np.max(np.abs(output)), CEILING + 1/32768)
        self.assertGreater(processor.limited_samples, 0)
        self.assertEqual(np.count_nonzero(output[2*RATE+320:]), 0)

    def test_continuous_quiet_tone_is_not_learned_into_a_hard_gate(self):
        t = np.arange(RATE * 3) / RATE
        samples = .002 * np.sin(2 * np.pi * 500 * t)
        output, _ = process(encode(samples))
        self.assertGreater(rms(output[RATE:]) / rms(samples[RATE:]), .75)
        self.assertGreater(np.corrcoef(samples[RATE:], output[RATE:])[0, 1], .98)

    def test_bad_pcm_rejected_and_new_holds_have_no_previous_tail(self):
        with self.assertRaises(ValueError):
            SpeechEnhancer().feed(b'\x01')
        process(encode(self.rng.normal(0, .02, RATE)))
        output, _ = process(bytes(322))
        self.assertEqual(np.count_nonzero(output), 0)


if __name__ == '__main__':
    unittest.main()
