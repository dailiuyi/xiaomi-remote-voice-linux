"""Replay captured ATVV v1.0 packets with two decoder-state policies.

This is an offline diagnostic, not a replacement microphone receiver.
The IMA ADPCM tables are standard codec constants. Post-processing models
ATVVoice v0.2.0's per-packet declip and triangle filter at 0 dB gain.
"""
import argparse
import json
from pathlib import Path

import numpy as np

from audio_filter import RATE, write_wav

STEPS = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34,
    37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494,
    544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552,
    1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026,
    4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
]
INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]


class Decoder:
    def __init__(self):
        self.reset()

    def reset(self, predictor=0, index=0):
        self.predictor = predictor
        self.index = min(88, max(0, index))

    def decode(self, payload):
        samples = []
        for byte in payload:
            for nibble in (byte >> 4, byte & 15):
                step = STEPS[self.index]
                difference = step >> 3
                for bit, amount in ((1, step >> 2), (2, step >> 1), (4, step)):
                    if nibble & bit:
                        difference += amount
                sign = -1 if nibble & 8 else 1
                self.predictor = min(32767, max(-32768, self.predictor + sign * difference))
                self.index = min(88, max(0, self.index + INDEX[nibble & 7]))
                samples.append(self.predictor)
        return samples


def postprocess(samples):
    # Integer division of signed values in Rust truncates toward zero.
    samples = samples.copy()
    for i in range(1, len(samples) - 1):
        previous, current, following = samples[i - 1:i + 2]
        dp, dn = abs(current - previous), abs(current - following)
        if dp > 1000 and dn > 1000 and min(dp, dn) > 2 * abs(following - previous):
            samples[i] = int((previous + following) / 2)
    original = samples.copy()
    for i in range(1, len(samples) - 1):
        samples[i] = (original[i - 1] + 2 * original[i] + original[i + 1]) >> 2
    return samples


def replay(paths, reset_on_start):
    decoder = Decoder()
    sessions = []
    session = None
    for path in paths:
        events = [json.loads(line) for line in path.read_text().splitlines()]
        if not events or events[-1]["kind"] != "end":
            raise ValueError("Capture is still running or lacks a clean end record")
        for event in events:
            payload = bytes.fromhex(event["hex"])
            if event["kind"] == "ctl" and payload:
                if payload[0] == 0x04:
                    if len(payload) != 4 or payload[2] != 2 or session is not None:
                        raise ValueError("Expected separate 16 kHz v1.0 sessions")
                    if reset_on_start:
                        decoder.reset()
                    session = {"capture": str(path), "stream_id": payload[3],
                               "start_ns": event["t_ns"], "arrivals_ns": [], "samples": [],
                               "initial_state": [decoder.predictor, decoder.index], "sync_count": 0}
                elif payload[0] == 0x0A and len(payload) >= 7:
                    decoder.reset(int.from_bytes(payload[4:6], "big", signed=True), payload[6])
                    if session is not None:
                        session["sync_count"] += 1
                elif payload[0] == 0x00 and session is not None:
                    session["stop_ns"] = event["t_ns"]
                    session["final_state"] = [decoder.predictor, decoder.index]
                    sessions.append(session)
                    session = None
            elif event["kind"] == "rx":
                if session is None:
                    raise ValueError("RX outside an AUDIO_START/STOP pair; ordering requires investigation")
                if len(payload) != 120:
                    raise ValueError("This experiment expects 120-byte headerless frames")
                session["arrivals_ns"].append(event["t_ns"])
                session["samples"].extend(postprocess(decoder.decode(payload)))
    if session is not None:
        raise ValueError("Incomplete session")
    return sessions


def metrics(session):
    samples = np.asarray(session["samples"], dtype=float) / 32768
    times = np.asarray(session["arrivals_ns"], dtype=float) / 1e9
    gaps_ms = np.diff(times) * 1000
    # Model only: each packet supplies 240 samples (=15 ms). No actual
    # PipeWire callback or queue-depth instrumentation is used here.
    lateness_ms = ((times - times[0]) - np.arange(len(times)) * 240 / RATE) * 1000
    jumps = np.flatnonzero(np.abs(np.diff(samples)) > 0.2) + 1
    return {
        "capture": session["capture"], "stream_id": session["stream_id"],
        "packets": len(times), "decoded_seconds": len(samples) / RATE,
        "control_session_seconds": (session["stop_ns"] - session["start_ns"]) / 1e9,
        "initial_state": session["initial_state"], "final_state": session["final_state"],
        "audio_sync_count": session["sync_count"],
        "mean_normalized_pcm": float(samples.mean()),
        "stable_mean_after_first_second": float(samples[RATE:].mean()),
        "peak_normalized_pcm": float(np.max(np.abs(samples))),
        "large_adjacent_jumps_above_0_2": len(jumps),
        "jump_times_seconds": (jumps / RATE).tolist(),
        "arrival_gap_ms_quantiles_50_90_99_max": np.quantile(gaps_ms, [.5, .9, .99, 1]).tolist(),
        "model_max_arrival_lateness_ms": float(max(0, lateness_ms.max())),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = {"sample_rate": RATE, "gain_db": 0,
              "limitations": ["Arrival timestamps are host D-Bus callback timestamps, not radio timestamps.",
                              "No PipeWire queue depth was captured; jitter model is not observed underrun count.",
                              "Direct packet replay omits wall-clock gaps; WAV duration follows decoded sample count.",
                              "Real microphone listening and a patched receiver still require verification."]}
    for policy in ("carry", "reset"):
        sessions = replay(args.captures, reset_on_start=policy == "reset")
        result[policy] = []
        for i, session in enumerate(sessions, start=1):
            target = args.output_dir / f"{policy}-session-{i}.wav"
            write_wav(target, np.asarray(session["samples"], dtype=float) / 32768)
            measurement = metrics(session)
            measurement["audio"] = str(target)
            result[policy].append(measurement)
    report = args.output_dir / "receiver-check.json"
    report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
