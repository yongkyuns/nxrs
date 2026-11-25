#!/usr/bin/env python3
"""Build and validate the public Rust API artifact consumed by the website."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
MARKER = '.nxrs-rust-api'
CACHE_MARKER = '.nxrs-rust-api-cargo'
MARKER_TEXT = 'NXRS public Rust API output\n'
SCHEMA = 1
SKIP_DIRECTORIES = {'.git', 'target', '__pycache__'}
DOC_FLAGS = '-D rustdoc::broken_intra_doc_links -D rustdoc::invalid_html_tags'
ENVIRONMENT_OVERRIDES = {
    'RUSTFLAGS', 'RUSTDOCFLAGS', 'CARGO_ENCODED_RUSTFLAGS',
    'CARGO_ENCODED_RUSTDOCFLAGS', 'RUSTC', 'RUSTDOC', 'RUSTC_WRAPPER',
    'RUSTC_WORKSPACE_WRAPPER', 'RUSTUP_TOOLCHAIN', 'RUSTC_BOOTSTRAP',
    'CARGO_BUILD_TARGET', 'CARGO_TARGET_DIR', 'CARGO_BUILD_RUSTC',
    'CARGO_BUILD_RUSTDOC', 'CARGO_BUILD_RUSTC_WRAPPER',
    'CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER', 'CARGO_BUILD_RUSTFLAGS',
    'CARGO_BUILD_RUSTDOCFLAGS',
}


def relative_path(value: str) -> Path:
    parts = PurePosixPath(value)
    if (not value or '\\' in value or parts.is_absolute()
            or any(p in ('', '.', '..') for p in parts.parts)
            or parts.as_posix() != value):
        raise ValueError(f'unsafe relative path: {value!r}')
    return Path(*parts.parts)


def no_links(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f'linked path is not an API build input or output: {path}')


def load_catalog(root: Path) -> dict:
    root = root.resolve()
    path = root / 'website/api.json'
    no_links(path)
    catalog = json.loads(path.read_text())
    packages, names, crates, groups = [], set(), set(), set()
    if not isinstance(catalog, dict) or not isinstance(catalog.get('title'), str) or not catalog['title'].strip():
        raise ValueError('API catalog needs a title')
    if not isinstance(catalog.get('groups'), list):
        raise ValueError('API catalog needs a list of groups')
    for group in catalog.get('groups', []):
        if not isinstance(group, dict):
            raise ValueError('API catalog groups must be objects')
        key = group.get('id', '')
        if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', key) or key in groups:
            raise ValueError('API catalog group IDs must be unique simple identifiers')
        groups.add(key)
        if (not isinstance(group.get('title'), str) or not group['title'].strip()
                or not isinstance(group.get('packages'), list) or not group['packages']):
            raise ValueError(f'{key}: an API group needs a title and packages')
        for package in group['packages']:
            if not isinstance(package, dict) or not all(isinstance(package.get(k), str) and package[k].strip()
                       for k in ('package', 'crate', 'path', 'summary')):
                raise ValueError(f'{key}: incomplete API package metadata')
            if (package['package'] in names or package['crate'] in crates
                    or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', package['crate'])):
                raise ValueError('API packages and Rust library names must be unique')
            directory = root / relative_path(package['path'])
            no_links(directory)
            no_links(directory / 'Cargo.toml')
            manifest = tomllib.loads((directory / 'Cargo.toml').read_text())
            actual = manifest['package']['name']
            library = manifest.get('lib', {}).get('name', actual.replace('-', '_'))
            source = directory / manifest.get('lib', {}).get('path', 'src/lib.rs')
            no_links(source)
            if (actual != package['package'] or library != package['crate']
                    or not source.is_file() or not source.resolve().is_relative_to(root)):
                raise ValueError(f"{package['package']}: catalog differs from the library manifest")
            names.add(package['package'])
            crates.add(package['crate'])
            packages.append(package)
    if not packages:
        raise ValueError('API catalog is empty')
    features = catalog.get('features')
    if (not isinstance(features, list) or not all(isinstance(f, str) for f in features)
            or len(features) != len(set(features))):
        raise ValueError('API features must be a unique list')
    by_name = {p['package']: p for p in packages}
    for value in features:
        if not isinstance(value, str) or value.count('/') != 1:
            raise ValueError('API features must use package/feature notation')
        package, feature = value.split('/')
        if package not in by_name:
            raise ValueError(f'feature selects an undocumented package: {value}')
        manifest = tomllib.loads((root / by_name[package]['path'] / 'Cargo.toml').read_text())
        if feature not in manifest.get('features', {}):
            raise ValueError(f'unknown API feature: {value}')
    return catalog | {'packages': packages}


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files_below(directory: Path, *, skip: set[str] = frozenset()) -> list[Path]:
    no_links(directory)
    if not directory.is_dir():
        raise ValueError(f'missing API directory: {directory}')
    files = []
    for current, directories, names in os.walk(directory, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in skip)
        for name in [*directories, *names]:
            path = Path(current) / name
            if path.is_symlink():
                raise ValueError(f'linked API content: {path}')
        for name in sorted(names):
            path = Path(current) / name
            if not path.is_file():
                raise ValueError(f'non-file API content: {path}')
            files.append(path)
    return sorted(files)


def source_inputs(root: Path, catalog: dict | None = None) -> dict[str, str]:
    """Include declared local dependency sources, including their build adapters."""
    root = root.resolve()
    catalog = load_catalog(root) if catalog is None else catalog
    workspace = tomllib.loads((root / 'Cargo.toml').read_text())
    pending = [root / p['path'] for p in catalog['packages']]
    for patches in workspace.get('patch', {}).values():
        pending.extend(root / spec['path'] for spec in patches.values()
                       if isinstance(spec, dict) and 'path' in spec)
    directories, inputs = set(), set()
    while pending:
        directory = pending.pop()
        no_links(directory)
        directory = directory.resolve()
        if not directory.is_relative_to(root):
            raise ValueError(f'API dependency source escapes the checkout: {directory}')
        if directory in directories:
            continue
        directories.add(directory)
        manifest = tomllib.loads((directory / 'Cargo.toml').read_text())
        inputs.update(files_below(directory, skip=SKIP_DIRECTORIES))
        sections = [manifest, *manifest.get('target', {}).values()]
        for section in sections:
            for key in ('dependencies', 'build-dependencies'):
                for name, value in section.get(key, {}).items():
                    if not isinstance(value, dict):
                        continue
                    if value.get('workspace'):
                        value = workspace.get('workspace', {}).get('dependencies', {}).get(name, {})
                        base = root
                    else:
                        base = directory
                    if isinstance(value, dict) and 'path' in value:
                        pending.append(base / value['path'])
        build = manifest.get('package', {}).get('build', 'build.rs')
        if isinstance(build, str) and (directory / build).exists():
            path = directory / build
            no_links(path)
            path = path.resolve()
            if not path.is_relative_to(root):
                raise ValueError('API build adapter escapes the checkout')
            inputs.add(path)
    for name in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml',
                 'website/api.json', 'website/build_api.py'):
        path = root / name
        no_links(path)
        if not path.is_file():
            raise ValueError(f'missing API input: {name}')
        inputs.add(path)
    config = root / '.cargo'
    if config.is_dir():
        inputs.update(files_below(config))
    return {path.relative_to(root).as_posix(): file_hash(path) for path in sorted(inputs)}


def toolchain(root: Path) -> str:
    return tomllib.loads((root / 'rust-toolchain.toml').read_text())['toolchain']['channel']


def validate_artifact(root: Path, artifact: Path, expected_revision: str | None = None) -> dict:
    root = root.resolve()
    no_links(artifact)
    artifact = artifact.resolve()
    for name in (MARKER, 'manifest.json'):
        no_links(artifact / name)
    if not (artifact / MARKER).is_file() or (artifact / MARKER).read_text() != MARKER_TEXT:
        raise ValueError('not an owned Rust API artifact')
    report = json.loads((artifact / 'manifest.json').read_text())
    catalog = load_catalog(root)
    if (not isinstance(report, dict) or report.get('schema') != SCHEMA or report.get('binding') != 'unbound'
            or report.get('toolchain') != toolchain(root)
            or report.get('features') != catalog['features']
            or report.get('packages') != catalog['packages']):
        raise ValueError('API artifact configuration differs from the checkout')
    if not isinstance(report.get('revision'), str) or not re.fullmatch(r'[0-9a-f]{40}', report['revision']):
        raise ValueError('API artifact has no exact source revision')
    if expected_revision is not None and report['revision'] != expected_revision:
        raise ValueError('API artifact revision differs from the website revision')
    if report.get('inputs') != source_inputs(root, catalog):
        raise ValueError('API artifact source inputs are stale')
    docs = artifact / 'docs'
    actual = {path.relative_to(docs).as_posix(): file_hash(path) for path in files_below(docs)}
    if not actual or actual != report.get('files'):
        raise ValueError('API artifact files are missing, changed, or unexpected')
    if report.get('output_bytes') != sum(path.stat().st_size for path in files_below(docs)):
        raise ValueError('API artifact size differs from its manifest')
    for package in catalog['packages']:
        if f"{package['crate']}/index.html" not in actual:
            raise ValueError(f"API artifact is missing {package['crate']}/index.html")
    for name in ('search-index.js', 'crates.js'):
        if name not in actual:
            raise ValueError(f'API artifact is missing rustdoc search support: {name}')
    return report


def owned_output(root: Path, path: Path, marker: str) -> Path:
    path = path.absolute()
    no_links(path)
    path = path.resolve()
    root = root.resolve()
    if (path == root or path in root.parents or path == root / 'target'
            or (path.is_relative_to(root) and not path.is_relative_to(root / 'target'))):
        raise ValueError('API output must be a dedicated target/ directory or an external build directory')
    no_links(path / marker)
    if path.exists() and (not path.is_dir()
                         or (any(path.iterdir()) and not (path / marker).is_file())):
        raise ValueError(f'refusing to replace unowned API output: {path}')
    if (path / marker).exists() and (path / marker).read_text() != MARKER_TEXT:
        raise ValueError(f'invalid API ownership marker: {path}')
    if path.exists():
        files_below(path)
    return path


def build_environment(source: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in source.items()
           if k not in ENVIRONMENT_OVERRIDES
           and not k.startswith(('NXRS_', 'NUTTX_', 'CARGO_CFG_', 'CARGO_FEATURE_'))
           and not re.fullmatch(r'CARGO_TARGET_.+_(RUSTFLAGS|RUSTDOCFLAGS|LINKER|RUNNER)', k)}
    # Empty overrides also suppress flags/wrappers from user Cargo configuration.
    env.update(CARGO_ENCODED_RUSTFLAGS='', RUSTC_WRAPPER='', RUSTC_WORKSPACE_WRAPPER='',
               RUSTDOCFLAGS=DOC_FLAGS)
    return env


def check_cargo_environment(root: Path, env: dict[str, str]) -> None:
    """Cargo [env] must not restore selectors or override sanitized compiler flags."""
    cargo_home = Path(env.get('CARGO_HOME', str(Path(env.get('HOME', str(Path.home()))) / '.cargo')))
    directories = [path / '.cargo' for path in (root, *root.parents)] + [cargo_home]
    for directory in dict.fromkeys(directories):
        # Cargo prefers the extensionless file if both names exist.
        path = directory / 'config'
        if not path.is_file():
            path = directory / 'config.toml'
        if not path.is_file():
            continue
        config = tomllib.loads(path.read_text())
        for name in config.get('env', {}):
            if (name in ENVIRONMENT_OVERRIDES
                    or name.startswith(('NXRS_', 'CARGO_CFG_', 'CARGO_FEATURE_'))
                    or re.fullmatch(r'CARGO_TARGET_.+_(RUSTFLAGS|RUSTDOCFLAGS|LINKER|RUNNER)', name)):
                raise ValueError(f'Cargo config must not inject API build selectors or compiler flags: {path} [{name}]')


def capture(args: list[str], root: Path, env: dict[str, str]) -> str:
    return subprocess.check_output(args, cwd=root, env=env, text=True).strip()


def build(root: Path, output: Path, cache: Path) -> dict:
    root = root.resolve()
    catalog = load_catalog(root)
    output = owned_output(root, output, MARKER)
    cache = owned_output(root, cache, CACHE_MARKER)
    if output.is_relative_to(cache) or cache.is_relative_to(output):
        raise ValueError('API artifact and Cargo cache directories must not overlap')
    inputs = source_inputs(root, catalog)
    env = build_environment(dict(os.environ))
    check_cargo_environment(root, env)
    pin = toolchain(root)
    rustc = capture(['rustup', 'which', '--toolchain', pin, 'rustc'], root, env)
    rustdoc = capture(['rustup', 'which', '--toolchain', pin, 'rustdoc'], root, env)
    compiler = capture([rustc, '-vV'], root, env)
    host = next(line.removeprefix('host: ') for line in compiler.splitlines()
                if line.startswith('host: '))
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', host):
        raise ValueError('invalid host target from the pinned compiler')
    revision = capture(['git', 'rev-parse', 'HEAD'], root, env)
    dirty = bool(capture(['git', 'status', '--porcelain'], root, env))
    env.update(RUSTUP_TOOLCHAIN=pin, RUSTC=rustc, RUSTDOC=rustdoc)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / CACHE_MARKER).write_text(MARKER_TEXT)
    docs = cache / host / 'doc'
    if docs.exists():
        no_links(docs)
        # Drop shared rustdoc indexes so removed catalog crates cannot survive.
        shutil.rmtree(docs)
    command = ['rustup', 'run', pin, 'cargo', 'doc', '--locked', '--lib', '--no-deps',
               '--manifest-path', str(root / 'Cargo.toml'), '--target', host,
               '--target-dir', str(cache)]
    for package in catalog['packages']:
        command.extend(['-p', package['package']])
    if catalog['features']:
        command.extend(['--features', ','.join(catalog['features'])])
    subprocess.run(command, cwd=root, env=env, check=True)
    if source_inputs(root, catalog) != inputs or capture(['git', 'rev-parse', 'HEAD'], root, env) != revision:
        raise ValueError('API source inputs changed during documentation generation')
    source_files = files_below(docs)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.nxrs-rust-api-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'artifact'
        staged.mkdir()
        shutil.copytree(docs, staged / 'docs')
        (staged / MARKER).write_text(MARKER_TEXT)
        report = {
            'schema': SCHEMA, 'revision': revision, 'dirty': dirty, 'toolchain': pin,
            'compiler': compiler, 'rustdoc': capture([rustdoc, '--version'], root, env),
            'host': host, 'binding': 'unbound', 'features': catalog['features'],
            'packages': catalog['packages'], 'inputs': inputs,
            'files': {p.relative_to(docs).as_posix(): file_hash(p) for p in source_files},
            'output_bytes': sum(p.stat().st_size for p in source_files),
        }
        (staged / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
        validate_artifact(root, staged, revision)
        if output.exists():
            shutil.rmtree(output)
        staged.rename(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'target/rust-api')
    parser.add_argument('--cargo-target', type=Path, default=ROOT / 'target/rust-api-cargo')
    args = parser.parse_args()
    report = build(ROOT, args.out, args.cargo_target)
    print(json.dumps({k: report[k] for k in ('revision', 'toolchain', 'host', 'binding', 'features')}
                     | {'packages': len(report['packages']), 'files': len(report['files']),
                        'output_bytes': report['output_bytes']}, indent=2))


if __name__ == '__main__':
    main()
