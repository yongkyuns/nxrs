#!/usr/bin/env python3
"""Enforce production layer boundaries using Cargo's resolved package metadata.

No --filter-platform: target-conditional/optional/build edges are all inspected.
Dev dependencies are deliberately separate from production dependencies.
This checks package dependencies, not arbitrary C includes or semantic behavior.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tomllib

ALLOWED = {
    'app': {'service', 'api', 'hal'},
    'service': {'service', 'api', 'hal'},
    'hal': {'api', 'backend', 'mock', 'driver', 'support'},
    'api': {'api'},
    'backend': {'api', 'driver', 'support'},
    'support': {'api'},
    'mock': {'api'},
    'driver': {'api', 'driver'},
    'platform': {'service', 'api', 'backend', 'mock', 'driver', 'support', 'platform'},
    'test': {'app', 'service', 'hal', 'api', 'backend', 'mock', 'driver', 'support', 'platform', 'test'},
}
PORTABLE = {'app', 'service', 'hal', 'api'}
# New third-party dependencies of portable production crates require a reviewed
# portability decision here. Do not infer portability merely from a crate name.
PORTABLE_EXTERNAL: set[str] = set()


def layer(path: Path) -> str:
    parts = path.parts
    if not parts:
        raise ValueError('a package cannot live at the virtual workspace root')
    if parts[0] == 'hal':
        if parts == ('hal', 'common'):
            return 'api'
        if parts == ('hal', 'support', 'nuttx'):
            return 'support'
        if len(parts) == 2 and parts[1] not in {'common', 'support', 'platform'}:
            return 'hal'
        if len(parts) == 3 and parts[1] not in {'api', 'native', 'nuttx', 'mock', 'common', 'support', 'platform'}:
            roles = {'api': 'api', 'mock': 'mock', 'native': 'backend', 'nuttx': 'backend', 'web': 'backend'}
            if parts[2] in roles:
                return roles[parts[2]]
        raise ValueError(f'unclassified HAL package: {path}')
    names = {'app': 'app', 'service': 'service', 'driver': 'driver',
             'platform': 'platform', 'tests': 'test'}
    if parts[0] not in names:
        raise ValueError(f'unclassified package location: {path}')
    return names[parts[0]]


def inspect(metadata: dict, root: Path) -> list[str]:
    """Return violations without shell execution; unit tests supply metadata."""
    root = root.resolve()
    errors: list[str] = []
    packages = {p['id']: p for p in metadata['packages']}
    members = set(metadata['workspace_members'])
    roles: dict[str, str] = {}
    paths: dict[str, Path] = {}
    by_path: dict[Path, str] = {}
    for identity in members:
        package = packages[identity]
        directory = Path(package['manifest_path']).resolve().parent
        try:
            relative = directory.relative_to(root)
            roles[identity] = layer(relative)
        except ValueError as error:
            errors.append(f"{package['name']}: {error}")
            continue
        paths[identity] = relative
        by_path[directory] = identity
    for identity, role in roles.items():
        package = packages[identity]
        for dep in package['dependencies']:
            # Cargo resolves aliases/workspace inheritance to the actual package.
            # Optional, target cfg and build edges are NOT exempted.
            if dep.get('kind') == 'dev':
                continue
            context = f"{package['name']} ({paths[identity]}) -> {dep['name']}"
            dep_path = dep.get('path')
            target = by_path.get(Path(dep_path).resolve()) if dep_path else None
            if target is not None:
                source, destination = paths[identity].parts, paths[target].parts
                if source == ('hal', 'common'):
                    errors.append(f'{context}: shared error values must remain dependency-free')
                if role == 'support' and destination != ('hal', 'common'):
                    errors.append(f'{context}: NuttX support may depend only on shared error values')
                if roles[target] == 'support' and role == 'backend' and source[-1] != 'nuttx':
                    errors.append(f'{context}: NuttX support belongs only to NuttX providers')
                if roles[target] not in ALLOWED[role]:
                    errors.append(f'{context}: forbidden {role} -> {roles[target]} edge')
            elif dep_path:
                errors.append(f'{context}: local dependency is outside classified workspace packages')
            elif role in PORTABLE and dep['name'] not in PORTABLE_EXTERNAL:
                errors.append(f'{context}: external dependency has not been qualified as portable')
    for identity in metadata.get('workspace_default_members', []):
        if identity not in paths:
            continue
        path = paths[identity]
        if (path.parts[:2] in {('tests', 'nuttx'), ('tests', 'nuttx-std')}
                or (path.parts[0] == 'hal' and len(path.parts) == 3 and path.parts[2] == 'nuttx')):
            errors.append(f'{path}: target-only package must not be a default member')
    return errors


def inspect_sources(metadata: dict, root: Path) -> list[str]:
    """Check the inventory and source-level markers without scanning dependencies."""
    root = root.resolve()
    errors: list[str] = []
    members = set(metadata['workspace_members'])
    manifests = {Path(p['manifest_path']).resolve() for p in metadata['packages'] if p['id'] in members}
    for name in ['app', 'service', 'hal', 'driver', 'platform', 'tests']:
        for directory, children, files in os.walk(root / name):
            children[:] = [child for child in children if child not in {'target', '.git'}]
            if 'Cargo.toml' not in files:
                continue
            manifest = Path(directory) / 'Cargo.toml'
            if manifest.resolve() in manifests:
                continue
            data = tomllib.loads(manifest.read_text())
            if (name == 'tests' and 'workspace' in data
                    and data.get('package', {}).get('publish') is False):
                # Optional experiments own separate dependency graphs. Production
                # path dependencies into them still fail inspect() above.
                children.clear()
                continue
            errors.append(f'{manifest.relative_to(root)}: package omitted from explicit workspace')
    for old in ['apps', 'crates']:
        if (root / old).exists():
            errors.append(f'{old}/: obsolete parallel hierarchy has returned')
    for package in metadata['packages']:
        if package['id'] not in members:
            continue
        directory = Path(package['manifest_path']).resolve().parent
        try:
            role = layer(directory.relative_to(root))
        except ValueError:
            continue
        if role not in PORTABLE:
            continue
        if role == 'app':
            source = directory / 'src/main.rs'
            text = source.read_text(encoding='utf-8') if source.is_file() else ''
            if not re.search(r'\bfn\s+main\s*\(', text) or '#![forbid(unsafe_code)]' not in text:
                errors.append(f"{package['name']}: app must own a safe handwritten src/main.rs")
        source = directory / 'src/lib.rs'
        if role == 'app' and not source.is_file():
            continue
        text = source.read_text(encoding='utf-8') if source.is_file() else ''
        markers = ['#![forbid(unsafe_code)]']
        if role in {'api', 'app', 'hal'}:
            markers.insert(0, '#![no_std]')
        for marker in markers:
            if marker not in text:
                qualifier = 'service library' if role == 'service' else 'portable library'
                errors.append(f"{package['name']}: {qualifier} must retain {marker}")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    command = ['cargo', 'metadata', '--locked', '--format-version=1', '--no-deps']
    try:
        result = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True, timeout=90)
        metadata = json.loads(result.stdout)
        errors = inspect(metadata, root) + inspect_sources(metadata, root)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        print(f'Architecture check could not complete: {error}', file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f'ERROR: {error}', file=sys.stderr)
        return 1
    print(f"PASS: {len(metadata['workspace_members'])} packages; production boundaries, inventory and portable markers")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
