#!/usr/bin/env python3
"""Build the pinned recording-only MediaMTX patch in a new private directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parents[1]/'upstream/mediamtx'


def build(output, go):
    pin = json.loads((HERE/'source.json').read_text())
    output = output.resolve()
    output.mkdir(mode=0o700)  # Never reuse a partial source/build directory.
    go = shutil.which(go)
    if go is None:
        raise ValueError('Go 1.21+ is required to select the pinned build toolchain')
    env = {**os.environ, 'GOTOOLCHAIN': pin['toolchain'], 'GOWORK': 'off',
           'GOSUMDB': 'sum.golang.org', 'GOFLAGS': '',
           'GOPATH': str(output/'gopath'), 'GOMODCACHE': str(output/'modules'),
           'GOCACHE': str(output/'cache'), 'PATH': str(Path(go).parent)+os.pathsep+os.environ['PATH']}

    def run(*args, cwd=output):
        return subprocess.run(args, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE,
                              text=True).stdout

    for name, module in pin['modules'].items():
        result = json.loads(run(go, 'mod', 'download', '-json', module['path']+'@'+module['version']))
        if result.get('Sum') != module['sum'] or result.get('Version') != module['version']:
            raise ValueError('Pinned native module checksum mismatch')
        source = Path(result['Dir']).resolve()
        source.relative_to(output/'modules')
        destination = output/name
        shutil.copytree(source, destination)
        for path in [destination, *destination.rglob('*')]:
            path.chmod(0o755 if path.is_dir() else 0o644)

    patch = HERE/'recorder-drain.patch'
    run('git', 'apply', '--check', str(patch))
    run('git', 'apply', str(patch))
    media = output/'mediamtx'
    with (media/'go.mod').open('a') as stream:
        stream.write('\nreplace github.com/bluenviron/gortsplib/v5 => ../gortsplib\n')
    run(go, 'generate', './internal/servers/hls', cwd=media)  # Upstream verifies its asset hash.
    (media/'internal/core/VERSION').write_text(pin['modules']['mediamtx']['version']+'-nxrs-recording1')
    binary = output/'nxrs-mediamtx-recording'
    run(go, 'build', '-trimpath', '-mod=readonly', '-o', str(binary), '.', cwd=media)
    run(go, 'mod', 'verify', cwd=media)
    print('Recording MediaMTX SHA256:', hashlib.sha256(binary.read_bytes()).hexdigest())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--go', default='go')
    args = parser.parse_args()
    build(args.output, args.go)
