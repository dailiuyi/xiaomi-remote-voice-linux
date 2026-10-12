"""One offline Qwen model, serialized by the caller, with shared speech VAD."""
import time
from pathlib import Path
import wave


class QwenTranscriber:
    def __init__(self, model_path):
        import torch
        from qwen_asr import Qwen3ASRModel
        from opencc import OpenCC
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        import numpy as np
        self.np, self.torch = np, torch
        self.vad, self.vad_options = get_speech_timestamps, VadOptions(
            min_silence_duration_ms=400, speech_pad_ms=200)
        torch.set_num_threads(8)
        self.model = Qwen3ASRModel.from_pretrained(str(model_path), dtype=torch.bfloat16,
                    device_map='cuda:0', attn_implementation='sdpa',
                    max_inference_batch_size=1, max_new_tokens=512)
        self.convert = OpenCC('t2s').convert

    def transcribe(self, pcm):
        if isinstance(pcm, (str, Path)):
            with wave.open(str(pcm), 'rb') as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                    raise ValueError('Expected 16kHz mono s16 WAV')
                pcm = w.readframes(w.getnframes())
        audio = self.np.frombuffer(pcm, dtype='<i2').astype(self.np.float32)/32768
        start = time.monotonic()
        segments = self.vad(audio, self.vad_options)
        speech = self.np.concatenate([audio[s['start']:s['end']] for s in segments]) if segments else None
        vad_seconds = time.monotonic()-start
        text = ''
        if speech is not None:
            text = self.convert(self.model.transcribe(audio=(speech, 16000), language='Chinese',
                context='中文软件开发口述，可能包含英文名称和编程术语。')[0].text.strip())
            self.torch.cuda.synchronize()
        return text, {'model': 'Qwen/Qwen3-ASR-1.7B', 'backend': 'Transformers BF16 SDPA',
                      'transcription_seconds': time.monotonic()-start,
                      'vad_seconds': vad_seconds, 'audio_seconds': len(audio)/16000,
                      'speech_seconds_after_vad': len(speech)/16000 if speech is not None else 0,
                      'torch_peak_allocated_mib': self.torch.cuda.max_memory_allocated()/2**20,
                      'torch_peak_reserved_mib': self.torch.cuda.max_memory_reserved()/2**20}
