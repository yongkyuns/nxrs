#!/usr/bin/env python3
"""Create and compile the guide's source project, without copying build outputs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / 'website/guide'


def block(page: str, tag: str, language: str) -> str:
    text = (GUIDE / page).read_text()
    pattern = rf'<!-- tutorial:{re.escape(tag)} -->\s*```{re.escape(language)}\n(.*?)\n```'
    found = re.findall(pattern, text, re.S)
    if len(found) != 1:
        raise ValueError(f'{page}: expected exactly one tutorial:{tag} {language} block')
    return found[0] + '\n'


def command(page: str, tag: str, project: Path) -> list[str]:
    return [part.replace('../my-firmware', str(project.resolve()))
            for part in shlex.split(block(page, tag, 'sh'))]


def environment() -> dict[str, str]:
    env = os.environ.copy()
    env['RUSTUP_TOOLCHAIN'] = tomllib.loads((ROOT / 'rust-toolchain.toml').read_text())['toolchain']['channel']
    for name in ('NXRS_PLATFORM', 'RUSTC', 'RUSTDOC', 'CARGO_BUILD_TARGET',
                 'CARGO_TARGET_DIR', 'RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS'):
        env.pop(name, None)
    return env


def run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    print('$ ' + shlex.join(args), flush=True)
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', flush=True)
    result.check_returncode()
    return result.stdout


def create_project(project: Path, env: dict[str, str]) -> None:
    if project.exists():
        raise FileExistsError(f'refusing to overwrite {project}')
    run(command('first-application.md', 'create', project), ROOT, env)
    (project / 'Cargo.toml').write_text(block('first-application.md', 'manifest', 'toml'))
    (project / 'src/main.rs').write_text(block('first-application.md', 'source', 'rust'))
    run(command('first-application.md', 'lock', project), ROOT, env)


def phases() -> list[tuple[str, str, str]]:
    return [
        ('hello', block('first-application.md', 'source', 'rust'), 'Hello from NXRS'),
        ('edited', block('firmware.md', 'modified-source', 'rust'), 'Hello again from NXRS'),
        ('worker', block('services.md', 'worker-source', 'rust'), 'Processed total: 6'),
    ]


def main() -> None:
    env = environment()
    report = {'toolchain': env['RUSTUP_TOOLCHAIN'], 'checks': []}
    with tempfile.TemporaryDirectory(prefix='nxrs-guide-') as directory:
        project = Path(directory) / 'my-firmware'
        create_project(project, env)
        for name, source, expected in phases():
            (project / 'src/main.rs').write_text(source)
            output = run(command('first-application.md', 'host', project), ROOT, env)
            if output.strip() != expected:
                raise RuntimeError(f'{name}: unexpected application output')
            report['checks'].append(f'cargo-new source project: {name} host execution')
        run(command('firmware.md', 'build', project) + ['--dry-run'], ROOT, env)
        report['checks'].append('external application accepted by NuttX build planner (dry run)')

    run(['cargo', 'test', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'website/examples/basic-firmware/Cargo.toml'), '--all-targets'], ROOT, env)
    report['checks'].append('source-matched worker lifecycle tests')
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
