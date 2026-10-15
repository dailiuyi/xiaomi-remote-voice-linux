"""Conservative, optional 16 kHz mono speech conditioning before cloud ASR.

This is a bounded DSP heuristic, not a speaker separator or a speech VAD.
Rolling low-energy spectra estimate stationary noise; attenuation is limited
to 6 dB and the noise estimate is capped to protect quiet speech. AGC increases
only on energy rises above the recent floor. No samples are gated or dropped.
"""
from collections import deque
import math

import numpy as np

RATE = 16000
HOP = 160
SIZE = 2 * HOP
WINDOW = np.sqrt(np.hanning(SIZE + 1)[:-1])
MAX_GAIN_DB = 12.0
CEILING = 10 ** (-2 / 20)


class SpeechEnhancer:
    """Incremental s16le processor; 10 ms lookahead, exact length on flush.

    A new instance is required for each hold. The first 200 ms collect noise
    statistics without spectral attenuation or gain increase. Digital silence
    is excluded from estimation: receiver padding is not room noise.
    """

    def __init__(self, *, denoise=True):
        self.denoise = denoise
        self.previous = np.zeros(HOP)
        self.overlap = np.zeros(SIZE)
        self.mask = np.ones(SIZE // 2 + 1)
        self.spectra = deque(maxlen=100)
        self.levels = deque(maxlen=100)
        self.noise = np.zeros(SIZE // 2 + 1)
        self.noise_rms = 0.0
        self.gain_db = 0.0
        self.limiter_gain = 1.0
        self.pending = bytearray()
        self.first = True
        self.closed = False
        self.input_samples = self.output_samples = self.frames = 0
        self.max_gain_db = 0.0
        self.limited_samples = 0
        self.input_square = self.output_square = 0.0

    def _block(self, current):
        frame = np.concatenate((self.previous, current))
        self.previous = current
        spectrum = np.fft.rfft(frame * WINDOW)
        power = np.abs(spectrum) ** 2
        # Only complete nonzero analysis frames count toward warmup/history.
        active = np.any(frame[:HOP]) and np.any(frame[HOP:])
        if active:
            self.spectra.append(power)
            self.levels.append(float(np.sqrt(np.mean(frame ** 2))))
            self.frames += 1
            if len(self.spectra) >= 20 and self.frames % 10 == 0:
                # A periodogram's 20th percentile is ~0.223 of its mean for
                # stationary Gaussian noise. Use half that mean estimate.
                estimate = np.quantile(self.spectra, .2, axis=0) / (-math.log(.8) * 2)
                # Do not classify persistent narrow speech harmonics as room
                # noise. This intentionally also leaves narrow tonal noise.
                neighbours = np.lib.stride_tricks.sliding_window_view(np.pad(estimate, (4, 4), mode='edge'), 9)
                estimate = np.minimum(estimate, 4 * np.median(neighbours, axis=1))
                # Parseval, with the two real-FFT endpoints counted once.
                energy = (2 * estimate.sum() - estimate[0] - estimate[-1]) / (SIZE * (WINDOW ** 2).sum())
                estimate *= min(1.0, 10 ** (-55 / 10) / max(energy, 1e-15))
                self.noise = estimate
                self.noise_rms = float(np.quantile(self.levels, .2))
        if self.denoise:
            target = np.sqrt(np.maximum(.25, 1 - self.noise / (power + 1e-15)))
            target = np.convolve(np.pad(target, (1, 1), mode='edge'), [.25, .5, .25], mode='valid')
            self.mask = .7 * self.mask + .3 * target
        self.overlap += np.fft.irfft(spectrum * self.mask, n=SIZE) * WINDOW
        output = self.overlap[:HOP].copy()
        self.overlap[:HOP] = self.overlap[HOP:]
        self.overlap[HOP:] = 0

        rms = float(np.sqrt(np.mean(output ** 2)))
        speech_candidate = (len(self.levels) >= 20 and
                            rms > max(10 ** (-58 / 20), 2 * self.noise_rms))
        desired = min(MAX_GAIN_DB, max(0.0, -30 - 20 * math.log10(max(rms, 1e-12)))) if speech_candidate else 0.0
        # Rise at <=24 dB/s; return toward unity at <=48 dB/s. Never boost
        # every pause just to reach a loudness target. No hard noise gate.
        next_gain = self.gain_db + float(np.clip(desired - self.gain_db, -.48, .24))
        gains = 10 ** (np.linspace(self.gain_db, next_gain, HOP) / 20)
        self.gain_db = next_gain
        self.max_gain_db = max(self.max_gain_db, next_gain)
        output *= gains
        # Immediate attack, 50 ms release; bound sudden clicks after AGC.
        release = math.exp(-1 / (RATE * .05))
        for i, value in enumerate(output):
            allowed = min(1.0, CEILING / max(abs(value), 1e-15))
            self.limiter_gain = min(allowed, 1 - (1 - self.limiter_gain) * release)
            if self.limiter_gain < .999:
                self.limited_samples += 1
            output[i] = value * self.limiter_gain
        return np.rint(output * 32768).astype('<i2').tobytes()

    def _process(self, payload):
        result = self._block(np.frombuffer(payload, dtype='<i2').astype(float) / 32768)
        if self.first:
            self.first = False
            return b''  # Compensate the WOLA delay, retaining the onset next hop.
        return result

    def _account(self, result):
        self.output_samples += len(result) // 2
        samples = np.frombuffer(result, dtype='<i2').astype(float) / 32768
        self.output_square += float(np.dot(samples, samples))
        return result

    def feed(self, pcm):
        if self.closed:
            raise ValueError('Processor is already flushed')
        if len(pcm) % 2:
            raise ValueError('Expected complete s16le samples')
        samples = np.frombuffer(pcm, dtype='<i2').astype(float) / 32768
        self.input_square += float(np.dot(samples, samples))
        self.input_samples += len(samples)
        self.pending.extend(pcm)
        output = bytearray()
        while len(self.pending) >= HOP * 2:
            output.extend(self._process(bytes(self.pending[:HOP * 2])))
            del self.pending[:HOP * 2]
        return self._account(bytes(output))

    def finish(self):
        if self.closed:
            return b''
        self.closed = True
        output = bytearray()
        if self.pending:
            output.extend(self._process(bytes(self.pending).ljust(HOP * 2, b'\0')))
            self.pending.clear()
        output.extend(self._process(bytes(HOP * 2)))
        remaining = (self.input_samples - self.output_samples) * 2
        return self._account(bytes(output[:remaining]))

    def metrics(self):
        def level(total, count):
            return round(10 * math.log10(max(total / max(count, 1), 1e-15)), 2)
        return {'mode': 'light' if self.denoise else 'gain',
                'algorithm': 'bounded-spectral-agc-v1', 'lookahead_ms': 10,
                'max_gain_db': round(self.max_gain_db, 2),
                'gain_limit_db': MAX_GAIN_DB, 'suppression_limit_db': 6.03,
                'noise_estimate_cap_dbfs': -55, 'peak_limit_dbfs': -2,
                'limited_samples': self.limited_samples,
                'input_samples': self.input_samples, 'output_samples': self.output_samples,
                'input_rms_dbfs': level(self.input_square, self.input_samples),
                'output_rms_dbfs': level(self.output_square, self.output_samples)}


def main():
    """Offline comparison only: no network, receiver, ASR, clipboard or keyboard."""
    import argparse
    import json
    from pathlib import Path
    import wave
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--mode', choices=('light', 'gain'), default='light')
    args = parser.parse_args()
    with wave.open(str(args.input), 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
            parser.error('Expected 16000 Hz mono s16 WAV')
        pcm = audio.readframes(audio.getnframes())
    processor = SpeechEnhancer(denoise=args.mode == 'light')
    result = b''.join(processor.feed(pcm[i:i+512]) for i in range(0, len(pcm), 512)) + processor.finish()
    # Exclusive creation protects both original recordings and earlier results.
    with args.output.open('xb') as target, wave.open(target, 'wb') as audio:
        audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(result)
    print(json.dumps(processor.metrics(), indent=2))


if __name__ == '__main__':
    main()
