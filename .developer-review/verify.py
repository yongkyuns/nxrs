"""Require the exact locally reviewed reference tree, not just a green render."""
from pathlib import Path
import json
import subprocess

EXPECTED = '33b14fabb7c986e4e725709559d5a59483990106'
BASELINE = '96c1d965598dba244d1b603753e261da4acf02f2'

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

subprocess.run(['git', 'add', 'docs/references'], check=True)
paths = git('diff', '--cached', '--name-only').splitlines()
assert len(paths) == 29 and all(p.startswith('docs/references/') for p in paths), 'Unexpected change scope'
root = git('write-tree')
reference_tree = git('rev-parse', f'{root}:docs/references')
assert reference_tree == EXPECTED, f'Reviewed tree mismatch: {reference_tree}'
old_svgs = [p for p in git('ls-tree', '-r', '--name-only', BASELINE, '--', 'docs/references').splitlines() if p.endswith('.svg')]
assert len(old_svgs) == 56
for path in old_svgs:
    assert git('hash-object', path) == git('rev-parse', f'{BASELINE}:{path}'), f'Existing SVG changed: {path}'
assert len(list(Path('docs/references').rglob('*.svg'))) == 59
out = Path('/tmp/developer-qualification')
out.mkdir(exist_ok=True)
report = {'reference_tree': reference_tree, 'changed_files': len(paths), 'existing_svgs_preserved': len(old_svgs), 'total_svgs': 59, 'browser_checks': 'Local results matched by exact reference tree; no fresh hosted browser run.'}
(out / 'qualification.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
