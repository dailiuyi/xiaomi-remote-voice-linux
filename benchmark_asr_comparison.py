"""Offline sequential ASR worker; never touches the running dictation service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
import wave

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'artifacts/asr-comparison'
PROMPT = '中文软件开发口述，可能包含英文名称和编程术语。'


def gpu_memory():
    text = subprocess.check_output(['nvidia-smi', '--query-compute-apps=pid,used_memory',
                                   '--format=csv,noheader,nounits'], text=True)
    for line in text.splitlines():
        pid, value = line.split(',')
        if pid.strip() == str(os.getpid()):
            return int(value.strip())
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('model', choices=['medium', '0.6B', '1.7B'])
    p.add_argument('--repeats', type=int, default=3)
    args = p.parse_args()
    import numpy as np
    from opencc import OpenCC
    from asr_comparison_metrics import score
    convert = OpenCC('t2s').convert
    dataset = json.loads((OUT/'dataset.json').read_text())
    references_path = OUT/'verified-references.json'
    verified = json.loads(references_path.read_text())['cases']
    result = {'model': args.model, 'pid': os.getpid(), 'prompt': PROMPT,
              'repeats': args.repeats, 'cases': [], 'memory_mib_observed': [],
              'memory_method': 'Own process NVML via nvidia-smi, sampled after load and each inference; not a continuous peak.',
              'dataset_sha256': hashlib.sha256((OUT/'dataset.json').read_bytes()).hexdigest()}
    result['verified_references_sha256'] = hashlib.sha256(references_path.read_bytes()).hexdigest()
    start = time.perf_counter()
    if args.model == 'medium':
        from faster_whisper import WhisperModel
        model = WhisperModel(str(ROOT/'artifacts/dictation-runtime/models/medium'),
                            device='cuda', compute_type='int8_float16',
                            local_files_only=True, cpu_threads=8)
        result['backend'] = 'faster-whisper/CTranslate2 int8_float16'

        def transcribe(audio):
            segments, _ = model.transcribe(audio, language='zh', beam_size=5, vad_filter=False,
                                           condition_on_previous_text=False, temperature=0,
                                           initial_prompt=PROMPT)
            return convert(''.join(s.text for s in segments).strip())

        def sync():
            pass  # CTranslate2 segment iteration completes inference synchronously.
    else:
        import torch
        from qwen_asr import Qwen3ASRModel
        torch.set_num_threads(8)
        model = Qwen3ASRModel.from_pretrained(str(OUT/'models'/args.model),
                    dtype=torch.bfloat16, device_map='cuda:0',
                    attn_implementation='sdpa', max_inference_batch_size=1, max_new_tokens=512)
        result['backend'] = 'qwen-asr/Transformers BF16 SDPA, batch=1'

        def transcribe(audio):
            return convert(model.transcribe(audio=(audio, 16000), language='Chinese', context=PROMPT)[0].text.strip())

        def sync():
            torch.cuda.synchronize()

    sync()
    result['model_load_seconds'] = time.perf_counter()-start
    result['memory_mib_observed'].append(gpu_memory())
    print('LOADED', args.model, result['model_load_seconds'], result['memory_mib_observed'], flush=True)
    for index, case in enumerate(dataset['cases']):
        case.update(verified[case['id']])
        path = Path(case['prepared'])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == case['prepared_sha256']
        with wave.open(str(path), 'rb') as stream:
            audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32)/32768
        if index == 0:
            transcribe(audio)
            sync()
            if args.model != 'medium':
                torch.cuda.reset_peak_memory_stats()
        runs = []
        for _ in range(args.repeats):
            sync()
            start = time.perf_counter()
            text = transcribe(audio)
            sync()
            seconds = time.perf_counter()-start
            runs.append({'text': text, 'seconds': seconds})
            result['memory_mib_observed'].append(gpu_memory())
        row = {'id': case['id'], 'source_seconds': case['source_seconds'],
               'prepared_seconds': case['prepared_seconds'], 'reference': case['reference'],
               'runs': runs, 'median_seconds': statistics.median(r['seconds'] for r in runs),
               'score': score(case['reference'], runs[0]['text'], case['terms']),
               'stable_transcript': len({r['text'] for r in runs}) == 1}
        result['cases'].append(row)
        if args.model != 'medium':
            result['torch_peak_allocated_mib'] = torch.cuda.max_memory_allocated()/2**20
            result['torch_peak_reserved_mib'] = torch.cuda.max_memory_reserved()/2**20
        (OUT/f'results-{args.model}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        print(case['id'], round(row['median_seconds'], 3), runs[0]['text'], flush=True)


if __name__ == '__main__':
    main()
