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
    camera = source_root/'hal/camera/src/api.rs'
    stream = source_root/'service/stream/src'
    stream_lib = stream/'lib.rs'
    wire = stream/'wire.rs'
    sources = [camera, stream_lib]
    if wire.is_file():
        sources.extend((wire, stream/'runtime/mod.rs', stream/'runtime/server.rs'))
    else:
        sources.append(stream/'server.rs')
    if not all(path.is_file() for path in sources):
        raise ValueError('camera sources required; use a PR merge or current-main worktree')
    output.mkdir(parents=True, exist_ok=True)
    compiler = ['rustup', 'run', '1.100.0', 'rustc', '--edition=2021', '-Dwarnings']
    def compile_file(source, name, dependencies=(), library=False, flags=()):
        args = compiler + list(flags) + ['--crate-name', name, str(source), '-L', 'dependency='+str(output)]
        for dependency in dependencies:
            args += ['--extern', f'{dependency}={output}/lib{dependency}.rlib']
        destination = output/('lib'+name+'.rlib' if library else name)
        if library:
            args += ['--crate-type', 'rlib']
        subprocess.run(args+['-o', str(destination)], check=True, timeout=60)
        return destination
    compile_file(camera, 'camera', library=True)
    dependencies = ['camera']
    legacy_wire = source_root/'service/capture-wire/src/lib.rs'
    if not wire.is_file() and legacy_wire.is_file():
        compile_file(legacy_wire, 'capture_wire', library=True)
        dependencies.append('capture_wire')
        sources.append(legacy_wire)
    flags = ('--cfg', 'feature="server"') if wire.is_file() else ()
    compile_file(stream_lib, 'stream_service', dependencies, library=True, flags=flags)
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
