"""Apply only the reviewed, checksummed documentation patch to the workbench."""
from pathlib import Path, PurePosixPath
import base64
import gzip
import hashlib
import subprocess

BASE = 'd3d4fd27135d3ca434ece15683b7de71651cac1e'
PAYLOAD = 'ccd5137f3607a4e18d5a0850ee2aae3a9643c8e30f660ca3887a2f9978df1377'

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

assert git('rev-parse', 'HEAD:docs/references') == BASE, 'Unexpected reference baseline'
parts = sorted((Path(__file__).parent / 'parts').glob('*.b64'))
assert [p.name for p in parts] == [f'{i:02}.b64' for i in range(9)], 'Missing or extra payload parts'
compressed = base64.b64decode(''.join(p.read_text() for p in parts), validate=True)
assert hashlib.sha256(compressed).hexdigest() == PAYLOAD, 'Transfer checksum mismatch'
patch = gzip.decompress(compressed)
headers = [line for line in patch.decode('utf-8').splitlines() if line.startswith('diff --git ')]
assert len(headers) == 26, 'Unexpected patch size'
for header in headers:
    prefix, suffix = header.removeprefix('diff --git ').split(' b/', 1)
    assert prefix.startswith('a/docs/references/') and prefix[2:] == suffix, 'Out-of-scope path or rename'
    path = PurePosixPath(suffix)
    assert not path.is_absolute() and '..' not in path.parts and path.suffix != '.svg', 'Unsafe or rendered path'
patch_path = Path('/tmp/developer-review.patch')
patch_path.write_bytes(patch)
options = ['--index', '--unidiff-zero', '--whitespace=error-all']
subprocess.run(['git', 'apply', '--check', *options, str(patch_path)], check=True)
subprocess.run(['git', 'apply', *options, str(patch_path)], check=True)
print('Applied 26 checksummed source/report files; three SVGs must be rendered.')
