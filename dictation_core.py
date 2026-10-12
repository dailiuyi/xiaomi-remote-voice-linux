"""Bounded push-to-talk capture, independent of desktop and ASR libraries."""
from enum import Enum


class CapturePhase(Enum):
    IDLE = 'idle'
    RECORDING = 'recording'
    TAIL = 'tail'
    TRANSCRIBING = 'transcribing'


class CaptureController:
    def __init__(self, tail_seconds=0.4, max_seconds=60):
        self.tail_seconds = tail_seconds
        self.max_bytes = int(max_seconds * 16000 * 2)
        self.phase = CapturePhase.IDLE
        self.ignored_hold = False
        self.parts = []
        self.byte_count = 0
        self.deadline = 0

    def on_state(self, state, now):
        if state == 'disconnected':
            was_active = self.phase in (CapturePhase.RECORDING, CapturePhase.TAIL)
            if was_active:
                self.complete()
            self.ignored_hold = False
            return 'cancelled' if was_active else None
        if self.ignored_hold:
            if state == 'connected':
                self.ignored_hold = False
            return 'busy' if state == 'streaming' else None
        if state == 'streaming':
            if self.phase == CapturePhase.RECORDING:
                return None  # Duplicate signal must not erase already collected PCM.
            if self.phase == CapturePhase.TAIL:
                self.complete()
                self.ignored_hold = True
                return 'overlap_cancelled'
            if self.phase != CapturePhase.IDLE:
                self.ignored_hold = True
                return 'busy'
            self.parts = []
            self.byte_count = 0
            self.phase = CapturePhase.RECORDING
            return 'recording'
        if state == 'connected' and self.phase == CapturePhase.RECORDING:
            self.phase = CapturePhase.TAIL
            self.deadline = now + self.tail_seconds
            return 'tail'
        return None

    def feed(self, pcm):
        if len(pcm) % 2:
            raise ValueError('Expected complete s16le samples')
        if self.phase not in (CapturePhase.RECORDING, CapturePhase.TAIL):
            return None
        if self.byte_count + len(pcm) > self.max_bytes:
            self.complete()
            self.ignored_hold = True
            return 'too_long'
        self.parts.append(pcm)
        self.byte_count += len(pcm)
        return None

    def tick(self, now):
        if self.phase != CapturePhase.TAIL or now < self.deadline:
            return None
        payload = b''.join(self.parts)
        self.parts = []
        self.byte_count = 0
        self.phase = CapturePhase.TRANSCRIBING
        return payload

    def complete(self):
        self.phase = CapturePhase.IDLE
        self.parts = []
        self.byte_count = 0
