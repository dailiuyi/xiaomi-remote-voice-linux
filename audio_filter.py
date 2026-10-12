"""Conservative 16 kHz mono filter for a Xiaomi remote microphone.

Independent DSP implementation; does not modify the upstream ATVVoice binary.
Noise calibration must contain held-button silence, never speech.
"""
from __future__ import annotations

import argparse
import json
import math
import signal
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

RATE = 16000
HOP = 256
SIZE = 512
WINDOW = np.sqrt(np.hanning(SIZE + 1)[:-1])


def read_wav(path):
    with wave.open(str(path), "rb") as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (RATE, 1, 2):
            raise ValueError("Expected 16000 Hz mono signed 16-bit WAV")
        return np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(float) / 32768


def write_wav(path, samples):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pcm = np.rint(np.clip(samples, -0.999, 0.999) * 32768).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(pcm.tobytes())


class HighPass:
    def __init__(self):
        self.alpha = math.exp(-2 * math.pi * 80 / RATE)
        self.previous_input = self.previous_output = 0.0

    def process(self, samples):
        result = np.empty(len(samples))
        for i, sample in enumerate(samples):
            value = self.alpha * (self.previous_output + sample - self.previous_input)
            result[i] = value
            self.previous_input, self.previous_output = sample, value
        return result


def suppress_impulses(samples):
    """Replace isolated large outliers; leave broad speech transients intact."""
    result = samples.copy()
    if len(samples) < 5:
        return result
    windows = np.lib.stride_tricks.sliding_window_view(samples, 5)
    median = np.median(windows, axis=1)
    center = samples[2:-2]
    mask = ((np.abs(center - median) > 0.20)
            & (np.abs(center - samples[1:-3]) > 0.18)
            & (np.abs(center - samples[3:-1]) > 0.18))
    result[2:-2] = np.where(mask, median, center)
    return result


def noise_profile(silence):
    if len(silence) < RATE:
        raise ValueError("At least one second of held-button silence is required")
    prepared = suppress_impulses(HighPass().process(silence))
    spectra = []
    for offset in range(0, len(prepared) - SIZE + 1, HOP):
        spectrum = np.fft.rfft(prepared[offset:offset + SIZE] * WINDOW)
        spectra.append(np.abs(spectrum) ** 2)
    # Median limits contamination from a button click; compensate the median
    # of exponentially distributed periodogram bins to estimate mean noise.
    return np.maximum(np.median(spectra, axis=0) / math.log(2), 1e-12)


class AudioFilter:
    """Streaming filter: one 256-sample hop, 16 ms algorithmic delay.

    The noise floor is fixed after explicit calibration, so speech cannot
    accidentally become the new background profile. No automatic gain increase.
    """
    def __init__(self, profile, output_gain=1.0):
        self.profile = np.asarray(profile, dtype=float)
        if self.profile.shape != (SIZE // 2 + 1,) or not np.isfinite(self.profile).all():
            raise ValueError("Invalid noise profile")
        self.output_gain = output_gain
        self.highpass = HighPass()
        self.previous = np.zeros(HOP)
        self.overlap = np.zeros(SIZE)
        self.previous_mask = np.ones(SIZE // 2 + 1)
        self.silent_hops = 4
        self.onset_position = SIZE

    def process(self, samples):
        if len(samples) != HOP:
            raise ValueError("A streaming block must contain 256 samples")
        # ATVVoice emits exact digital silence outside a button-held session.
        # Ramp only the beginning of a new session (32 ms), not every quiet
        # speech pause, to soften the opening discontinuity without deleting it.
        active = bool(np.any(samples != 0))
        if active and self.silent_hops >= 4:
            self.onset_position = 0
        self.silent_hops = 0 if active else self.silent_hops + 1
        if active and self.onset_position < SIZE:
            positions = np.arange(HOP) + self.onset_position
            ramp = np.sin(np.minimum(positions / SIZE, 1.0) * math.pi / 2) ** 2
            samples = samples * ramp
            self.onset_position += HOP
        current = self.highpass.process(samples)
        frame = suppress_impulses(np.concatenate((self.previous, current)))
        self.previous = current
        spectrum = np.fft.rfft(frame * WINDOW)
        power = np.abs(spectrum) ** 2
        # A 0.2 floor bounds attenuation at roughly 14 dB. Frequency and time
        # smoothing reduce the metallic 'musical noise' of hard spectral gates.
        mask = np.sqrt(np.maximum(0.04, 1 - 1.5 * self.profile / (power + 1e-12)))
        mask = np.convolve(np.pad(mask, (1, 1), mode="edge"), [0.25, 0.5, 0.25], mode="valid")
        mask = 0.65 * self.previous_mask + 0.35 * mask
        self.previous_mask = mask
        filtered = np.fft.irfft(spectrum * mask, n=SIZE) * WINDOW
        self.overlap += filtered
        result = self.overlap[:HOP].copy() * self.output_gain
        self.overlap[:-HOP] = self.overlap[HOP:]
        self.overlap[-HOP:] = 0
        # Unity slope for normal speech, smooth compression above 0.65.
        absolute = np.abs(result)
        compressed = 0.65 + 0.17 * np.tanh(np.maximum(absolute - 0.65, 0) / 0.17)
        return np.sign(result) * np.where(absolute > 0.65, compressed, absolute)


def filter_audio(samples, profile, output_gain=1.0):
    processor = AudioFilter(profile, output_gain)
    padding = (-len(samples)) % HOP
    padded = np.pad(samples, (0, padding + HOP))
    output = np.concatenate([processor.process(padded[i:i + HOP])
                             for i in range(0, len(padded), HOP)])
    return output[HOP:HOP + len(samples)]


def db_rms(samples):
    return float(20 * np.log10(max(float(np.sqrt(np.mean(samples ** 2))), 1e-12)))


def process_file(args):
    samples = read_wav(args.input)
    nonzero = np.flatnonzero(samples)
    if not len(nonzero):
        raise ValueError("The recording contains only silence")
    start, end = int(nonzero[0]), int(nonzero[-1]) + 1
    clip = samples[start:end]
    calibration_start = int(args.noise_offset * RATE)
    calibration_end = calibration_start + int(args.noise_seconds * RATE)
    if calibration_end >= len(clip):
        raise ValueError("Recording is too short for calibration plus speech")
    silence = clip[calibration_start:calibration_end]
    profile = noise_profile(silence)
    result = filter_audio(clip, profile, args.gain)
    output = Path(args.output)
    write_wav(output, result)
    raw_path = output.with_name(output.stem + "-raw.wav")
    write_wav(raw_path, clip)
    profile_path = output.with_suffix(".profile.json")
    profile_path.write_text(json.dumps({"rate": RATE, "hop": HOP,
                                      "noise_power": profile.tolist()}, indent=2) + "\n")
    quiet_output = result[calibration_start:calibration_end]
    report = {
        "input": str(args.input), "raw_clip": str(raw_path), "processed": str(output),
        "noise_profile": str(profile_path), "clip_seconds": len(clip) / RATE,
        "noise_interval_in_clip": [args.noise_offset, args.noise_offset + args.noise_seconds],
        "output_gain": args.gain, "noise_rms_before_dbfs": db_rms(silence),
        "noise_rms_after_dbfs": db_rms(quiet_output),
        "measured_noise_reduction_db": db_rms(silence) - db_rms(quiet_output),
        "raw_full_scale_samples": int(np.count_nonzero(np.abs(clip) >= 32767 / 32768)),
        "processed_full_scale_samples": int(np.count_nonzero(np.abs(result) >= 32767 / 32768)),
        "raw_peak_dbfs": float(20 * np.log10(max(np.max(np.abs(clip)), 1e-12))),
        "processed_peak_dbfs": float(20 * np.log10(max(np.max(np.abs(result)), 1e-12))),
        "listening_acceptance": "pending",
        "calibration_assumption": "the nominated noise interval contains held-button silence without speech",
    }
    output.with_suffix(".report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def live(args):
    profile = json.loads(Path(args.profile).read_text())["noise_power"]
    processor = AudioFilter(profile, args.gain)
    # Refuse absent endpoints: never fall back to the system microphone/speakers.
    sources = subprocess.check_output(["pactl", "list", "sources", "short"], text=True)
    sinks = subprocess.check_output(["pactl", "list", "sinks", "short"], text=True)
    if args.source not in [row.split("\t")[1] for row in sources.splitlines()]:
        raise ValueError("Requested source is absent")
    if args.sink not in [row.split("\t")[1] for row in sinks.splitlines()]:
        raise ValueError("Requested sink is absent")
    capture = subprocess.Popen(["pw-record", "--raw", "--target=" + args.source,
                                "--rate=16000", "--channels=1", "--format=s16", "--latency=256",
                                "--properties=node.dont-fallback=true", "-"],
                               stdout=subprocess.PIPE)
    playback = subprocess.Popen(["pw-cat", "--playback", "--raw", "--target=" + args.sink,
                                 "--rate=16000", "--channels=1", "--format=s16", "--latency=256",
                                 "--properties=node.dont-fallback=true", "-"],
                                stdin=subprocess.PIPE)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            payload = capture.stdout.read(HOP * 2)
            if len(payload) != HOP * 2:
                break
            block = np.frombuffer(payload, dtype="<i2").astype(float) / 32768
            cleaned = processor.process(block)
            playback.stdin.write(np.rint(cleaned * 32768).astype("<i2").tobytes())
            playback.stdin.flush()
    except KeyboardInterrupt:
        pass
    finally:
        for child in (capture, playback):
            child.terminate()
        for child in (capture, playback):
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    offline = commands.add_parser("process")
    offline.add_argument("input", type=Path)
    offline.add_argument("output", type=Path)
    offline.add_argument("--noise-offset", type=float, default=0.5)
    offline.add_argument("--noise-seconds", type=float, default=2.0)
    offline.add_argument("--gain", type=float, default=1.0)
    offline.set_defaults(handler=process_file)
    streaming = commands.add_parser("live")
    streaming.add_argument("--source", required=True)
    streaming.add_argument("--sink", required=True)
    streaming.add_argument("--profile", required=True)
    streaming.add_argument("--gain", type=float, default=1.0)
    streaming.set_defaults(handler=live)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
