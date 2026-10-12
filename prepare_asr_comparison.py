"""Freeze references and prepare identical VAD audio for all benchmark models."""
import hashlib
import json
from pathlib import Path
import time
import wave
import numpy as np
from faster_whisper.vad import get_speech_timestamps, VadOptions

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'artifacts/asr-comparison'
new = OUT / 'recordings/20261012-065725'
old = ROOT / 'artifacts/2026-10-12-native-receiver'
cases = [
    ('receiver-1', old/'live-session-1-native.wav', '这是小米遥控器接收层测试，请继续检查当前项目。', []),
    ('receiver-2', old/'live-session-2-native.wav', '这是小米遥控器接收层测试，请继续检查当前项目。', []),
    ('accepted-input', ROOT/'artifacts/dictation-runtime/sessions/20261012-062029/001.wav', '请检查当前项目，先运行测试，再继续修复代码。', []),
    ('coding-1', new/'001.wav', '请让 Codex 检查 GitHub 上的当前分支，先运行测试，再修复代码。', ['Codex', 'GitHub', '分支']),
    ('coding-2', new/'002.wav', '创建一个 Python 文件，文件名是 main 点 py，再更新 README 点 md。', ['Python', 'main.py', 'README.md']),
    ('coding-3', new/'003.wav', '把端口从八零八零改成一八七六七，超时时间设为三十秒。', ['8080', '18767', '30秒']),
    ('coding-4', new/'004.wav', '先运行 npm install，再运行 npm run dev；不要推送或合并，保留当前未提交的修改。', ['npm install', 'npm run dev', '不要推送', '未提交']),
]
prepared = OUT / 'prepared'
prepared.mkdir(exist_ok=True)
manifest = {'reference_evidence': 'User confirmed all four coding sentences were read verbatim; earlier prompts fix the other three references.',
            'vad': {'min_silence_duration_ms': 400, 'speech_pad_ms': 200}, 'cases': []}
for name, path, ref, terms in cases:
    with wave.open(str(path), 'rb') as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2')
    start = time.perf_counter()
    segments = get_speech_timestamps(pcm.astype(np.float32)/32768, VadOptions(**manifest['vad']))
    if not segments:
        raise ValueError(f'No speech in {path}')
    kept = np.concatenate([pcm[s['start']:s['end']] for s in segments])
    seconds = time.perf_counter()-start
    dest = prepared / f'{name}.wav'
    with wave.open(str(dest), 'wb') as w:
        w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        w.writeframes(kept.tobytes())
    case = {'id': name, 'source': str(path), 'prepared': str(dest), 'reference': ref, 'terms': terms,
            'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'prepared_sha256': hashlib.sha256(dest.read_bytes()).hexdigest(),
            'source_seconds': len(pcm)/16000, 'prepared_seconds': len(kept)/16000,
            'vad_seconds': seconds, 'vad_segments': segments}
    manifest['cases'].append(case)
    print(name, case['source_seconds'], case['prepared_seconds'], flush=True)
dest = OUT / 'dataset.json'
if dest.exists() and json.loads(dest.read_text())['cases'] != manifest['cases']:
    raise RuntimeError('Dataset already frozen; do not overwrite it')
dest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
