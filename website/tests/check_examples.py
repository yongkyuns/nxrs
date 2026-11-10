#!/usr/bin/env python3
"""Compile and run the documentation's dependency-free Rust examples."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    print('$ ' + ' '.join(command), flush=True)
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', flush=True)
    result.check_returncode()
    return result.stdout


def main() -> None:
    env = os.environ.copy()
    channel = tomllib.loads((ROOT / 'rust-toolchain.toml').read_text())['toolchain']['channel']
    env['RUSTUP_TOOLCHAIN'] = channel
    env.pop('NXRS_PLATFORM', None)
    report = {'toolchain': channel, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='nxrs-guide-') as directory:
        project = Path(directory) / 'my-firmware'
        shutil.copytree(ROOT / 'website/examples/basic-firmware', project,
                        ignore=shutil.ignore_patterns('target'))
        manifest = str(project / 'Cargo.toml')
        base = ['cargo', 'run', '--locked', '--offline', '--manifest-path', manifest]
        output = run(base + ['--bin', 'hello'], project, env)
        if output.strip() != 'Hello from NXRS':
            raise RuntimeError('unexpected hello output')
        output = run(base + ['--example', 'worker'], project, env)
        if output.strip() != 'Processed total: 6':
            raise RuntimeError('unexpected worker output')
        run(['cargo', 'test', '--locked', '--offline', '--manifest-path', manifest, '--all-targets'], project, env)
        report['checks'].append('copied independent workspace: hello, worker and lifecycle unit tests')

    # Exercise the same public commands shown by the guide on the real checkout.
    output = run(['cargo', 'run', '--locked', '-p', 'nxrs-std-demo', '--', '--list'], ROOT, env)
    if 'channels' not in output.splitlines() or 'deadlines' not in output.splitlines():
        raise RuntimeError('missing documented standard-library recipes')
    output = run(['cargo', 'run', '--locked', '-p', 'nxrs-std-demo', '--', '--case', 'channels'], ROOT, env)
    if 'STD_DEMO PASS cases=1' not in output:
        raise RuntimeError('standard-library channel example did not complete')
    report['checks'].append('documented std-demo list and channel commands')
    report['result'] = 'passed'
    output_path = ROOT / 'target/website-examples.json'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
