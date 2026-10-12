"""Render reproducible benchmark results using user-verified references."""
import csv
import json
from pathlib import Path
import statistics
from asr_comparison_metrics import score

ROOT = Path(__file__).resolve().parent / 'artifacts/asr-comparison'
refs = json.loads((ROOT/'verified-references.json').read_text())['cases']
summary = []
rows = []
details = []
for name in ('medium', '0.6B', '1.7B'):
    result = json.loads((ROOT/f'results-{name}.json').read_text())
    measured = []
    for case in result['cases']:
        ref = refs[case['id']]
        metrics = score(ref['reference'], case['runs'][0]['text'], ref['terms'])
        row = {'model': name, 'id': case['id'], 'reference': ref['reference'],
               'transcript': case['runs'][0]['text'], 'median_seconds': case['median_seconds'],
               'min_seconds': min(r['seconds'] for r in case['runs']),
               'max_seconds': max(r['seconds'] for r in case['runs']),
               'stable_transcript': case['stable_transcript'], **metrics}
        rows.append(row)
        if case['id'] != 'coding-1':
            measured.append(row)
    total = sum(r['reference_chars'] for r in measured)
    errors = sum(r['edit_errors'] for r in measured)
    hits = sum(sum(r['term_matches'].values()) for r in measured)
    terms = sum(len(r['term_matches']) for r in measured)
    mem = [v for v in result['memory_mib_observed'] if v is not None]
    item = {'model': name, 'clips_scored': len(measured), 'reference_chars': total,
            'edit_errors': errors, 'normalized_cer': errors/total,
            'term_hits': hits, 'term_total': terms,
            'median_asr_seconds': statistics.median(r['median_seconds'] for r in measured),
            'total_asr_seconds': sum(r['median_seconds'] for r in measured),
            'load_seconds': result['model_load_seconds'],
            'max_observed_process_mib': max(mem) if mem else None,
            'torch_peak_allocated_mib': result.get('torch_peak_allocated_mib'),
            'torch_peak_reserved_mib': result.get('torch_peak_reserved_mib'),
            'backend': result['backend']}
    summary.append(item)
    details.append(f"### {name}\n\n" + '\n\n'.join(
        f"**{r['id']}** · {r['median_seconds']:.3f} 秒 · 错误 {r['edit_errors']}/{r['reference_chars']}\n\n参考：{r['reference']}\n\n转写：{r['transcript']}"
        for r in rows if r['model'] == name))
data = {'summary': summary, 'rows': rows, 'scoring_exclusion': 'coding-1 long recording contains test instructions; exact wording not separately confirmed. It is displayed but excluded from formal aggregate.'}
(ROOT/'comparison.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
with (ROOT/'comparison.csv').open('w') as f:
    writer = csv.DictWriter(f, fieldnames=['model', 'id', 'reference', 'transcript', 'median_seconds', 'normalized_cer', 'edit_errors', 'reference_chars'])
    writer.writeheader()
    writer.writerows({k:r[k] for k in writer.fieldnames} for r in rows)
table = '| 模型 | 字符错误率 | 错误/参考字符 | 术语命中 | 单段转写中位数 | 本进程显存最大观测值 |\n|---|---:|---:|---:|---:|---:|\n'
for s in summary:
    table += f"| {s['model']} | {s['normalized_cer']:.1%} | {s['edit_errors']}/{s['reference_chars']} | {s['term_hits']}/{s['term_total']} | {s['median_asr_seconds']:.3f} s | {s['max_observed_process_mib']} MiB |\n"
text = '# MI RC003-MS 本地 ASR 同音频对比\n\n' + table + '\n'
text += ('测试主机：elma-ubuntu-pc，RTX 4060 Laptop 8GB。原有语音输入服务全程保留，后台负载不是完全隔离。'
         '全部音频来自同一遥控器及已验收接收算法，16kHz 单声道 PCM。模型离线运行，语音未上传。\n\n'
         '7 段音频统一先用 faster-whisper Silero VAD 去静默（400ms 最短静默、200ms padding），每种模型读取相同 SHA256 的处理版。'
         '参考文本按用户确认的实际分段校正；第一段包含四句和说明，说明的逐字内容未独立确认，因此该长段只展示，不计入正式总分。\n\n'
         '正式分数来自其余 6 段。字符错误率统一忽略标点、空格、英文大小写，等价转换汉语数字和口述文件名里的“点”。'
         '不修正专有名词或错别字；英文按字符计分。术语命中是归一化字符串出现率，不能证明原始代码格式可以直接执行。'
         '原文和转写全文列于下方。\n\n'
         '每个模型先用同一短段预热一次，再对每段执行 3 次；表中单段耗时是各段 3 次中位数的中位数。'
         '计时包括特征处理、模型推理和文本输出，不含公共 VAD、录音、松键等待或粘贴。'
         '显存是 nvidia-smi 本进程在加载后和每次推理后的最大观测值，不能视为连续采样峰值；Qwen另有 PyTorch 分配/保留峰值。\n\n'
         'Whisper：medium，CTranslate2 int8_float16，中文、beam=5、temperature=0、无前文条件。'
         'Qwen：0.6B/1.7B，Transformers BF16、SDPA、batch=1、max_new_tokens=512、中文。'
         '两者使用同一泛化的软件开发上下文，不提供参考句或专项词表。后端和精度不同，这里比较当前机器上具体可运行方案。\n\n'
         '小样本结果不代表通用正确率；未测微信输入法、流式首字延迟、长时间常驻和真实输入框体验。'
         '此次未更换正在使用的模型。模型版本、音频哈希、VAD区间、原始逐次结果和依赖冻结记录均保存在同目录。\n\n')
text += '\n\n'.join(details)+'\n'
(ROOT/'REPORT.md').write_text(text)
print(json.dumps(summary, ensure_ascii=False, indent=2))
