"""Pinned official model/registry downloads with verified resumable ranges."""
import concurrent.futures
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import time
import urllib.request

ROOT = Path(__file__).resolve().parent / 'artifacts/asr-comparison'


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def download(file, direct=False):
    target = Path(file['target'])
    target.parent.mkdir(parents=True, exist_ok=True)
    def valid(path):
        if not path.exists() or path.stat().st_size != file['size']:
            return False
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == file['sha256']
    if valid(target):
        print('already verified', target, flush=True)
        return
    if file['size'] < 8 * 1024 * 1024:
        open_url = urllib.request.build_opener(urllib.request.ProxyHandler({})).open if direct else urllib.request.urlopen
        with open_url(file['url'], timeout=120) as source, target.open('wb') as out:
            shutil.copyfileobj(source, out)
    else:
        # One sparse destination avoids doubling disk use with assembled parts.
        partial = target.with_suffix(target.suffix + '.partial')
        marker = target.with_suffix(target.suffix + '.ranges.json')
        step = 64 * 1024 * 1024
        ranges = [(lo, min(file['size'] - 1, lo + step - 1))
                  for lo in range(0, file['size'], step)]
        done = set(json.loads(marker.read_text())) if marker.exists() and partial.exists() else set()
        with partial.open('ab') as stream:
            stream.truncate(file['size'])
        def fetch(item):
            index, (lo, hi) = item
            if index in done:
                return index
            for attempt in range(4):
                try:
                    request = urllib.request.Request(file['url'], headers={'Range': f'bytes={lo}-{hi}'})
                    open_url = urllib.request.build_opener(urllib.request.ProxyHandler({})).open if direct else urllib.request.urlopen
                    with open_url(request, timeout=90) as source, partial.open('r+b') as out:
                        if source.status != 206 or not source.headers.get('Content-Range', '').startswith(f'bytes {lo}-{hi}/'):
                            raise RuntimeError('Server did not honor exact range')
                        out.seek(lo)
                        count = 0
                        while chunk := source.read(256 * 1024):
                            if count + len(chunk) > hi - lo + 1:
                                raise RuntimeError('Oversized range')
                            out.write(chunk)
                            count += len(chunk)
                        if count != hi - lo + 1:
                            raise RuntimeError('Incomplete range')
                    return index
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(2 * (attempt + 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for future in concurrent.futures.as_completed([pool.submit(fetch, item) for item in enumerate(ranges)]):
                done.add(future.result())
                marker.write_text(json.dumps(sorted(done)))
                print(target.name, len(done), '/', len(ranges), flush=True)
        if not valid(partial):
            raise RuntimeError('SHA256/size verification failed: ' + str(target))
        partial.rename(target)
    if not valid(target):
        raise RuntimeError('Download verification failed: ' + str(target))
    print('VERIFIED', target, file['sha256'], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--modelscope-direct', action='store_true')
    args = parser.parse_args()
    if args.modelscope_direct:
        manifest = json.loads((ROOT / 'model-manifest.json').read_text())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for size in ('0.6B', '1.7B'):
            repo = 'Qwen/Qwen3-ASR-' + size
            url = f'https://modelscope.cn/api/v1/models/{repo}/repo/files?Revision=master&Recursive=true'
            with opener.open(url, timeout=60) as response:
                entries = {f['Path']: f for f in json.load(response)['Data']['Files']}
            for file in manifest:
                if file['repository'] != repo:
                    continue
                name = Path(file['target']).name
                entry = entries[name]
                if entry['Sha256'] != file['sha256'] or entry['Size'] != file['size']:
                    raise RuntimeError('Official mirrors differ: ' + name)
                file['original_huggingface_url'] = file['url']
                file['modelscope_file_revision'] = entry['Revision']
                file['url'] = f'https://modelscope.cn/models/{repo}/resolve/{entry["Revision"]}/{name}'
        (ROOT / 'modelscope-download-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        for file in manifest:
            download(file, direct=True)
        return
    manifest = []
    for size, revision in [('0.6B', '5eb144179a02acc5e5ba31e748d22b0cf3e303b0'),
                           ('1.7B', '7278e1e70fe206f11671096ffdd38061171dd6e5')]:
        repo = 'Qwen/Qwen3-ASR-' + size
        tree = get_json(f'https://huggingface.co/api/models/{repo}/tree/{revision}')
        for entry in tree:
            name = entry['path']
            if name in ('.gitattributes', 'README.md') or entry['type'] != 'file':
                continue
            url = f'https://huggingface.co/{repo}/resolve/{revision}/{name}'
            digest = entry.get('lfs', {}).get('oid')
            if digest is None:
                with urllib.request.urlopen(url, timeout=60) as r:
                    digest = hashlib.sha256(r.read()).hexdigest()
            manifest.append({'repository': repo, 'revision': revision, 'url': url,
                             'size': entry['size'], 'sha256': digest,
                             'target': str(ROOT / 'models' / size / name)})
    (ROOT / 'model-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    # Models are downloaded sequentially; each large file uses bounded parallel ranges.
    for file in manifest:
        download(file)


if __name__ == '__main__':
    main()
