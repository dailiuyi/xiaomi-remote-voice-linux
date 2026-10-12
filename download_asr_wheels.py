"""Download pinned GPU runtime wheels from PyPI using bounded verified ranges."""
import json
import re
from download_asr_comparison import ROOT, get_json, download

specs = [('torch', '2.9.1'), ('torchaudio', '2.9.1'), ('torchvision', '0.24.1')]
torch = get_json('https://pypi.org/pypi/torch/2.9.1/json')
for requirement in torch['info']['requires_dist']:
    match = re.match(r'((?:nvidia-[a-z0-9-]+)|triton)==([\d.]+)', requirement)
    if match:
        specs.append(match.groups())
manifest = []
for name, version in specs:
    metadata = get_json(f'https://pypi.org/pypi/{name}/{version}/json')
    compatible = [f for f in metadata['urls'] if f['filename'].endswith('.whl')
                  and 'x86_64' in f['filename'] and 'manylinux' in f['filename']
                  and ('cp312' in f['filename'] or 'py3-none' in f['filename'])]
    if len(compatible) != 1:
        raise RuntimeError(f'Expected one compatible wheel: {name}: {[f["filename"] for f in compatible]}')
    file = compatible[0]
    manifest.append({'name': name, 'version': version, 'url': file['url'],
                     'size': file['size'], 'sha256': file['digests']['sha256'],
                     'target': str(ROOT/'runtime/downloads'/file['filename'])})
(ROOT/'runtime/wheel-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
for file in manifest:
    download(file)
