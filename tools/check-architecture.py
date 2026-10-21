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
    'hal': {'api', 'hal', 'driver', 'support'},
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
        if len(parts) == 2 and parts[1] not in {'common', 'support', 'platform'}:
            return 'hal'
        raise ValueError(f'HAL must be one capability crate, not a nested contract/provider package: {path}')
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
                    errors.append(f'{context}: shared radio delivery contracts must remain dependency-free')
                if destination == ('hal', 'common') and role == 'hal' and source[-1] not in {'wifi', 'ble'}:
                    errors.append(f'{context}: device errors belong to the capability; common is radio delivery only')
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
        if path.parts[:2] in {('tests', 'nuttx'), ('tests', 'nuttx-std')}:
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
        has_binary = any('bin' in target.get('kind', []) for target in package.get('targets', [{'kind': ['bin']}]))
        if role == 'app' and has_binary:
            source = directory / 'src/main.rs'
            text = source.read_text(encoding='utf-8') if source.is_file() else ''
            if not re.search(r'\bfn\s+main\s*\(', text) or '#![forbid(unsafe_code)]' not in text:
                errors.append(f"{package['name']}: app must own a safe handwritten src/main.rs")
        source = directory / 'src/lib.rs'
        if role == 'app' and has_binary and not source.is_file():
            continue
        text = source.read_text(encoding='utf-8') if source.is_file() else ''
        markers = ['#![deny(unsafe_code)]' if role == 'hal' else '#![forbid(unsafe_code)]']
        if role in {'api', 'hal'}:
            markers.insert(0, '#![no_std]')
        for marker in markers:
            if marker not in text:
                qualifier = 'service library' if role == 'service' else 'portable library'
                errors.append(f"{package['name']}: {qualifier} must retain {marker}")
        if role == 'hal':
            data = tomllib.loads((directory / 'Cargo.toml').read_text())
            features = data.get('features', {})
            if set(features) & {'native', 'mock', 'nuttx'}:
                errors.append(f"{package['name']}: provider features retired; select one platform")
            for module in ('api.rs', 'controller.rs'):
                path = directory / 'src' / module
                if path.is_file() and '#![forbid(unsafe_code)]' not in path.read_text():
                    errors.append(f"{package['name']}: shared {module} must forbid unsafe code")
    return errors


def inspect_hal_boundaries(root: Path) -> list[str]:
    """Reject platform selection in shared Rust and unreviewed native sources.

    The inventory permits native ABI glue only. Entries still require semantic
    code review; scalar declarations do not certify a mechanical boundary.
    """
    errors = []
    shared = {'api.rs', 'controller.rs', 'worker.rs', 'events.rs', 'record.rs',
              'udp.rs', 'file.rs', 'recording.rs', 'replay.rs', 'client.rs',
              'gatt_backend.rs'}
    for path in (root / 'hal').rglob('*.rs'):
        relative = path.relative_to(root).as_posix()
        # SDK callbacks are ABI operations. These retired high-level symbols
        # instead connected Rust modules to Rust modules through a C protocol.
        text = re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)
        if re.search(r'\bfn\s+(?:nxrs_host_wifi_\w+|nxrs_host_ble_scan_\w+|'
                     r'nxrs_ble_gatt_(?:open|submit|poll|recover|close)|'
                     r'nxrs_ble_peer_lookup(?:_rust)?)\s*\(', text):
            errors.append(f'{relative}: internal HAL calls must use typed Rust, not a C protocol')
        if (path.name not in shared and '/shared/' not in relative
                and not relative.endswith('providers/gatt.rs')):
            continue
        # Comments may describe the rule without becoming platform selection.
        if (re.search(r'\b(?:target_(?:os|env|family|vendor)|nxrs_provider|NXRS_PLATFORM)\b', text)
                or re.search(r'(?:#\s*\[\s*cfg(?:_attr)?\s*\(|\bcfg!\s*\()[^;{}]*\b(?:unix|windows)\b', text)):
            errors.append(f'{relative}: shared Rust behavior must not select a platform/provider')
    inventory = root / 'hal/ffi-boundaries.toml'
    entries = tomllib.loads(inventory.read_text()).get('bridge', []) if inventory.is_file() else []
    declared = {}
    for entry in entries:
        path = entry.get('path', '')
        if path in declared:
            errors.append(f'{path}: duplicate native boundary entry')
        declared[path] = entry
    actual = {path.relative_to(root).as_posix(): path for path in (root / 'hal').rglob('*')
              if path.is_file() and path.suffix in {'.c', '.h', '.m', '.mm', '.cc', '.cpp'}}
    for path in sorted(declared.keys() - actual.keys()):
        errors.append(f'{path}: stale native boundary entry')
    ordinary = r'\b(open|read|write|lseek|ftruncate|fsync|socket|send|recv|close|malloc|calloc|realloc|pthread_create|pthread_mutex_lock|pthread_mutex_trylock|clock_gettime|usleep|nanosleep|g_thread_try_new|g_usleep|g_get_monotonic_time)\s*\('
    for relative, path in actual.items():
        if not re.fullmatch(r'hal/[^/]+/src/providers/(?:[^/]+/)+ffi/.+', relative):
            errors.append(f'{relative}: native ABI glue must live beside its owning Rust provider')
        entry = declared.get(relative)
        if not entry:
            errors.append(f'{relative}: native source requires a documented ABI boundary')
            continue
        if not entry.get('reason', '').strip():
            errors.append(f'{relative}: native boundary needs a reason')
        if entry.get('kind') == 'migration':
            errors.append(f'{relative}: C behavior exceptions are forbidden; implement behavior in Rust')
        elif entry.get('kind') == 'abi':
            text = re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)
            forbidden = set(re.findall(ordinary, text)) - set(entry.get('allowed_calls', []))
            if forbidden:
                errors.append(f'{relative}: ordinary I/O/runtime behavior belongs in Rust: {sorted(forbidden)}')
        else:
            errors.append(f'{relative}: unknown native boundary kind')
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    command = ['cargo', 'metadata', '--locked', '--format-version=1', '--no-deps']
    try:
        result = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True, timeout=90)
        metadata = json.loads(result.stdout)
        errors = inspect(metadata, root) + inspect_sources(metadata, root) + inspect_hal_boundaries(root)
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
