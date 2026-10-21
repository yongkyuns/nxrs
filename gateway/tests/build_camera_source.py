#!/usr/bin/env python3
"""Compile the repository's actual camera contract and stream server, without MCU providers.

The PR merge checkout includes camera work from main. On an older feature-only
checkout, pass --source-root pointing to a separate current-main worktree. No
source is downloaded, rewritten or copied into a competing implementation.
"""
import argparse
import hashlib
from pathlib import Path
import subprocess


def build(source_root, output):
    source_root, output = Path(source_root).resolve(), Path(output).resolve()
    sources = [source_root/'hal/camera/src/api.rs', source_root/'service/stream/src/lib.rs',
               source_root/'service/stream/src/server.rs']
    if not all(path.is_file() for path in sources):
        raise ValueError('camera sources required; use a PR merge or current-main worktree')
    output.mkdir(parents=True, exist_ok=True)
    compiler = ['rustup', 'run', '1.90.0', 'rustc', '--edition=2021', '-Dwarnings']
    def compile_file(source, name, dependencies=(), library=False):
        args = compiler + ['--crate-name', name, str(source), '-L', 'dependency='+str(output)]
        for dependency in dependencies:
            args += ['--extern', f'{dependency}={output}/lib{dependency}.rlib']
        destination = output/('lib'+name+'.rlib' if library else name)
        if library:
            args += ['--crate-type', 'rlib']
        subprocess.run(args+['-o', str(destination)], check=True, timeout=60)
        return destination
    compile_file(sources[0], 'camera', library=True)
    compile_file(sources[1], 'stream_service', ['camera'], library=True)
    result = compile_file(Path(__file__).with_name('camera_source.rs'), 'nxrs_camera_source',
                          ['camera', 'stream_service'])
    for source in sources:
        print('ACTUAL CAMERA SOURCE:', source.relative_to(source_root),
              hashlib.sha256(source.read_bytes()).hexdigest(), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.source_root, args.output)
