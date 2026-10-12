"""Signal-level checks; these do not establish real-device sound quality."""
import unittest

import numpy as np

from audio_filter import RATE, HOP, AudioFilter, filter_audio, noise_profile, db_rms


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(73)
        self.background = self.rng.normal(0, 0.012, RATE * 3)
        self.profile = noise_profile(self.background)

    def test_stationary_noise_is_reduced_without_muting(self):
        noise = self.rng.normal(0, 0.012, RATE * 2)
        result = filter_audio(noise, self.profile)
        reduction = db_rms(noise[RATE:]) - db_rms(result[RATE:])
        self.assertGreater(reduction, 6)
        self.assertGreater(np.std(result[RATE:]), 0.0001)

    def test_voice_band_retains_amplitude_and_length(self):
        t = np.arange(RATE * 2) / RATE
        speech = 0.12 * np.sin(2 * np.pi * 600 * t) + 0.06 * np.sin(2 * np.pi * 1700 * t)
        result = filter_audio(speech + self.rng.normal(0, 0.012, len(t)), self.profile)
        self.assertEqual(len(result), len(speech))
        gain = np.dot(result[RATE:], speech[RATE:]) / np.dot(speech[RATE:], speech[RATE:])
        self.assertGreater(gain, 0.75)
        self.assertGreater(np.corrcoef(result[RATE:], speech[RATE:])[0, 1], 0.95)

    def test_single_sample_pop_and_broad_overload_are_bounded(self):
        samples = np.zeros(RATE)
        samples[8000] = 0.99
        samples[12000:12200] = 0.99
        result = filter_audio(samples, self.profile, 4)
        self.assertLess(np.max(np.abs(result[7900:8100])), 0.20)
        self.assertLessEqual(np.max(np.abs(result)), 0.82 + 1e-12)

    def test_silence_has_no_nan_or_residual_output(self):
        result = filter_audio(np.zeros(RATE + 17), self.profile)
        self.assertTrue(np.isfinite(result).all())
        self.assertEqual(np.count_nonzero(result), 0)

    def test_opening_transient_is_softened_without_deleting_later_speech(self):
        samples = np.zeros(RATE)
        samples[100:112] = 0.99
        samples[1000:] = 0.1 * np.sin(2 * np.pi * 700 * np.arange(RATE - 1000) / RATE)
        result = filter_audio(samples, self.profile)
        self.assertLess(np.max(np.abs(result[:500])), 0.15)
        self.assertGreater(np.std(result[4000:]), 0.05)

    def test_chunked_processing_matches_offline_alignment(self):
        samples = self.rng.normal(0, 0.03, HOP * 20)
        processor = AudioFilter(self.profile)
        live = [processor.process(samples[i:i + HOP]) for i in range(0, len(samples), HOP)]
        live.append(processor.process(np.zeros(HOP)))
        live = np.concatenate(live)[HOP:]
        np.testing.assert_allclose(live, filter_audio(samples, self.profile), atol=1e-12)


if __name__ == "__main__":
    unittest.main()
