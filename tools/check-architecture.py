#!/usr/bin/env python3
"""Enforce production layer boundaries using Cargo's resolved package metadata.

No --filter-platform: target-conditional/optional/build edges are all inspected.
Dev dependencies are deliberately separate from production dependencies.
This checks package dependencies, not arbitrary C includes or semantic behavior.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path
import subprocess
import sys
import tomllib

ALLOWED = {
    'app': {'service', 'api', 'hal', 'shared'},
    'service': {'service', 'api', 'hal', 'shared'},
    'hal': {'api', 'hal', 'driver', 'support', 'shared'},
    'api': {'api'},
    'backend': {'api', 'driver', 'support'},
    'support': {'api'},
    'mock': {'api'},
    'driver': {'api', 'driver', 'shared'},
    'shared': {'shared'},
    'build-support': {'build-support'},
    'platform': {'service', 'api', 'backend', 'mock', 'driver', 'support', 'platform', 'shared', 'build-support'},
    'test': {'app', 'service', 'hal', 'api', 'backend', 'mock', 'driver', 'support', 'platform', 'test', 'shared', 'build-support'},
}
PORTABLE = {'app', 'service', 'hal', 'api', 'shared'}
PROVIDER_FEATURES = {'native', 'mock', 'nuttx', 'std', 'linux', 'macos', 'windows'}
# New third-party dependencies of portable production crates require a reviewed
# portability decision here. Do not infer portability merely from a crate name.
PORTABLE_EXTERNAL: set[str] = set()
# Optional detector model packages: MicroFlow generates no_std quantized kernels,
# nalgebra has no std backend, and zune-jpeg is a single-threaded scalar decoder.
# Native PICO compute is private to the selected HAL implementation. Model/SDK
# provenance and qualification live in hal/detector/README.md; services do not
# receive a general third-party dependency exception.
PORTABLE_EXTERNAL_BY_PACKAGE = {
    # Embedded bus traits for the IMU's NuttX I2C resource adapter. Chip protocol
    # dependencies belong to the independent driver package.
    'nxrs-imu': {'embedded-hal'},
    # Pinned no_std UBX codec; fixed buffers, no alloc/std/serial/runtime features.
    'nxrs-gnss': {'kinavis-ubx'},
    'nxrs-detector': {'microflow', 'nalgebra', 'zune-jpeg'},
    # Fixed-capacity camera/gateway wire protocol, with bounded JSON buffers.
    # serde is safe Rust; serde_json uses the already-required Rust std I/O.
    # The actual NuttX publisher is qualified with the camera product image.
    'nxrs-detection-wire': {'serde', 'serde_json'},
}


def layer(path: Path) -> str:
    parts = path.parts
    if not parts:
        raise ValueError('a package cannot live at the virtual workspace root')
    if parts == ('platform', 'build-support'):
        return 'build-support'
    if parts[0] == 'hal':
        if parts == ('hal', 'common'):
            return 'api'
        if len(parts) == 3 and parts[-1] == 'build-support':
            return 'build-support'
        if len(parts) == 2 and parts[1] not in {'common', 'support', 'platform'}:
            return 'hal'
        raise ValueError(f'HAL must be one capability crate, not a nested contract/provider package: {path}')
    names = {'app': 'app', 'service': 'service', 'driver': 'driver', 'shared': 'shared',
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
                dep_path = dep.get('path')
                destination = by_path.get(Path(dep_path).resolve()) if dep_path else None
                if destination and ((role in {'app', 'service'} and roles[destination] in {'platform', 'build-support'})
                                    or (role == 'platform' and roles[destination] == 'app')):
                    errors.append(f"{package['name']}: test dependencies cannot bypass the app/platform boundary")
                if (destination and role in {'hal', 'driver', 'build-support'}
                        and paths[destination] == Path('platform/build')):
                    errors.append(f"{package['name']}: HAL and driver tooling must not depend on the build controller")
                if destination and role in {'hal', 'driver'} and roles[destination] == 'build-support':
                    errors.append(f"{package['name']}: HAL and driver build helpers must be build dependencies")
                continue
            context = f"{package['name']} ({paths[identity]}) -> {dep['name']}"
            dep_path = dep.get('path')
            target = by_path.get(Path(dep_path).resolve()) if dep_path else None
            if target is not None:
                source, destination = paths[identity].parts, paths[target].parts
                if source == ('hal', 'common'):
                    errors.append(f'{context}: shared radio identities must remain dependency-free')
                if destination == ('hal', 'common') and role == 'hal' and source[-1] not in {'wifi', 'ble'}:
                    errors.append(f'{context}: common contains radio identities and bounded payloads only')
                # A HAL/driver can use generic host helpers or its own SDK build
                # package. These are build edges only, never target libraries.
                if (role in {'hal', 'driver'} and dep.get('kind') == 'build'
                        and (destination == ('platform', 'build-support')
                             or (role == 'hal' and destination == (*source, 'build-support')))):
                    continue
                if (source == ('platform', 'build-support') and destination[0] == 'hal'):
                    errors.append(f'{context}: generic build support must not depend on capability-specific tooling')
                if roles[target] not in ALLOWED[role]:
                    errors.append(f'{context}: forbidden {role} -> {roles[target]} edge')
            elif dep_path:
                errors.append(f'{context}: local dependency is outside classified workspace packages')
            elif (role in PORTABLE and dep['name'] not in PORTABLE_EXTERNAL
                  and dep['name'] not in PORTABLE_EXTERNAL_BY_PACKAGE.get(package['name'], set())):
                errors.append(f'{context}: external dependency has not been qualified as portable')
    for identity in metadata.get('workspace_default_members', []):
        if identity not in paths:
            continue
        path = paths[identity]
        if path.parts[:2] in {('tests', 'nuttx'), ('tests', 'nuttx-std'), ('platform', 'nuttx')}:
            errors.append(f'{path}: target-only package must not be a default member')
    return errors


def requires_test(attributes: str) -> bool:
    """Recognize attributes that cannot enable an item in a production build."""
    def condition(expression):
        expression = expression.strip()
        if expression == 'test':
            return True
        group = re.fullmatch(r'(all|any)\s*\((.*)\)', expression, flags=re.S)
        if not group:
            return False
        arguments, start, depth = [], 0, 0
        body = group[2]
        for index, char in enumerate(body):
            depth += (char == '(') - (char == ')')
            if char == ',' and depth == 0:
                arguments.append(body[start:index])
                start = index + 1
        arguments.append(body[start:])
        conditions = [condition(value) for value in arguments if value.strip()]
        return any(conditions) if group[1] == 'all' else all(conditions)

    for attribute in re.findall(r'#\s*\[([^\]]*)\]', attributes, flags=re.S):
        cfg = re.fullmatch(r'\s*cfg\s*\((.*)\)\s*', attribute, flags=re.S)
        if cfg and condition(re.sub(r'"[^"\n]*"', 'value', cfg[1])):
            return True
    return False


def inspect_source_ownership(directory: Path, root: Path, owners: set[Path]) -> list[str]:
    """Rust source belongs to its Cargo package; test witnesses remain explicit."""
    errors = []
    manifest = directory / 'Cargo.toml'
    build = tomllib.loads(manifest.read_text()).get('package', {}).get('build')
    if isinstance(build, str) and not (directory / build).resolve().is_relative_to(directory):
        errors.append(f'{manifest.relative_to(root)}: build scripts must belong to their Cargo package')
    attributes = r'(?:#\s*\[[^\]]*\]\s*)+'
    module = re.compile(r'(' + attributes + r')(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*;', re.S)
    literal_include = re.compile(r'\binclude!\s*\(\s*"([^"\n]+)"\s*,?\s*\)')
    manifest_include = re.compile(
        r'\binclude!\s*\(\s*concat!\s*\(\s*env!\s*\(\s*"CARGO_MANIFEST_DIR"\s*\)'
        r'((?:\s*,\s*"[^"\n]*")+)\s*,?\s*\)\s*,?\s*\)')
    for path in directory.rglob('*.rs'):
        relative = path.relative_to(directory)
        if 'target' in relative.parts or '.git' in relative.parts or relative.parts[0] == 'tests':
            continue
        # Nested SDK build packages own their own sources and are checked once.
        if next(parent for parent in path.parents if parent in owners) != directory:
            continue
        text = re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)
        references = []
        for item in module.finditer(text):
            for value in re.findall(r'\bpath\s*=\s*"([^"\n]+)"', item[1]):
                references.append(((path.parent / value).resolve(), item[1]))
        for item in literal_include.finditer(text):
            guard = re.search(r'(' + attributes + r')$', text[:item.start()], flags=re.S)
            references.append(((path.parent / item[1]).resolve(), guard[1] if guard else ''))
        for item in manifest_include.finditer(text):
            suffix = ''.join(re.findall(r'"([^"\n]*)"', item[1]))
            guard = re.search(r'(' + attributes + r')$', text[:item.start()], flags=re.S)
            references.append((Path(str(directory) + suffix).resolve(), guard[1] if guard else ''))
        for source, guard in references:
            owner = next((parent for parent in source.parents if parent in owners), None)
            if source.is_relative_to(directory) and owner in {None, directory}:
                continue
            if source.is_relative_to(root / 'tests') and requires_test(guard):
                continue
            errors.append(f'{path.relative_to(root)}: cross-package Rust source imports must use Cargo dependencies')
    return errors


def inspect_sources(metadata: dict, root: Path) -> list[str]:
    """Check the inventory and source-level markers without scanning dependencies."""
    root = root.resolve()
    errors: list[str] = inspect_dependency_capsule(root)
    members = set(metadata['workspace_members'])
    manifests = {Path(p['manifest_path']).resolve() for p in metadata['packages'] if p['id'] in members}
    owners = {manifest.parent for manifest in manifests}
    for name in ['app', 'service', 'hal', 'driver', 'platform', 'tests', 'shared']:
        for directory, children, files in os.walk(root / name):
            children[:] = [child for child in children if child not in {'target', '.git'}]
            if 'Cargo.toml' not in files:
                continue
            manifest = Path(directory) / 'Cargo.toml'
            data = tomllib.loads(manifest.read_text())
            # Cargo features may compose optional functionality. They must not
            # introduce a second platform selector or enable probes implicitly.
            provider_features = set(data.get('features', {})) & PROVIDER_FEATURES
            if provider_features:
                errors.append(f'{manifest.relative_to(root)}: provider features retired; select one platform: {sorted(provider_features)}')
            for feature, values in data.get('features', {}).items():
                if feature != 'probe' and re.search(r'(?:^|[-_])probe(?:[-_]|$)', feature):
                    errors.append(f'{manifest.relative_to(root)}: use probe for diagnostics, not {feature}')
                if feature != 'probe' and any(value == 'probe' or value.endswith('/probe') for value in values):
                    errors.append(f'{manifest.relative_to(root)}: probe must remain an explicit opt-in, independent of other features')
            if manifest.resolve() in manifests:
                continue
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
        if role != 'test':
            errors.extend(inspect_source_ownership(directory, root, owners))
        if role in {'app', 'service', 'shared'}:
            data = tomllib.loads((directory / 'Cargo.toml').read_text())
            manifest_text = (directory / 'Cargo.toml').read_text()
            if data.get('target') or re.search(r'["\'](?:nuttx|linux|macos|windows)["\']', manifest_text):
                errors.append(f"{package['name']}: app/service manifests must not select targets or providers")
            native = {'.c', '.h', '.cc', '.cpp', '.m', '.mm', '.hpp'}
            for path in directory.rglob('*'):
                if path.is_file() and path.suffix in native and 'target' not in path.relative_to(directory).parts:
                    errors.append(f'{path.relative_to(root)}: native code belongs below the app/service boundary')
            sources = [directory / 'build.rs', *directory.rglob('*.rs')]
            for path in set(sources):
                if not path.is_file() or 'target' in path.relative_to(directory).parts:
                    continue
                text = re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)
                identity = (r'\b(?:target_(?:os|arch|env|family|vendor)|hal_enabled|hal_mock|nxrs_hal_platform|nxrs_provider|NXRS_PLATFORM)\b'
                            r'|(?:std|core)::(?:os|arch)::|std::env::consts::(?:OS|ARCH|FAMILY)|\blibc::'
                            r'|(?:#\s*\[\s*cfg(?:_attr)?\s*\(|\bcfg!\s*\()[^;{}]*\b(?:unix|windows)\b'
                            r'|["\']/(?:dev|proc)/|["\'](?:nuttx|linux|macos|windows)["\']')
                hidden_source = r'(?:#\[path|include!)[^;\n]*["\'][^"\']*(?:platform/|hal/[^/]+/src/platform/)'
                if re.search(identity, text) or re.search(hidden_source, text):
                    errors.append(f'{path.relative_to(root)}: app/service code must have no platform knowledge')
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
        if role in {'api', 'hal', 'shared'}:
            markers.insert(0, '#![no_std]')
        for marker in markers:
            if marker not in text:
                qualifier = 'service library' if role == 'service' else 'portable library'
                errors.append(f"{package['name']}: {qualifier} must retain {marker}")
        if role in {'service', 'app'}:
            data = tomllib.loads((directory / 'Cargo.toml').read_text())
            for dep in package['dependencies']:
                if (dep.get('kind') == 'dev' or not dep.get('path')
                        or Path(dep['path']).resolve() != root / 'hal/detector'):
                    continue
                alias = dep.get('rename') or dep['name']
                forwarded = [value.split('/', 1)[1]
                             for values in data.get('features', {}).values() for value in values
                             if '/' in value and value.split('/', 1)[0].rstrip('?') == alias]
                if dep.get('features') or forwarded:
                    errors.append(f"{package['name']}: enable detector models through profile-derived product wiring, not an app/service manifest")
        if role == 'hal':
            for module in ('api.rs', 'controller.rs'):
                path = directory / 'src' / module
                if path.is_file() and '#![forbid(unsafe_code)]' not in path.read_text():
                    errors.append(f"{package['name']}: shared {module} must forbid unsafe code")
    identities = set()
    app_directories = set()
    entry_symbols = set()
    for package in metadata['packages']:
        directory = Path(package['manifest_path']).resolve().parent
        if package['id'] not in members or directory.relative_to(root).parts[0] != 'app':
            continue
        data = tomllib.loads((directory / 'Cargo.toml').read_text())
        identities.update({directory.name, package['name']})
        app_directories.add(directory.name)
        command = data.get('package', {}).get('metadata', {}).get('nxrs', {}).get('firmware', {}).get('command')
        if command:
            identities.add(command)
            entry_symbols.add(command + "_main")
    for path in (root / 'platform').rglob('*'):
        if not path.is_file() or path.suffix not in {'.rs', '.py', '.sh', '.toml', '.c', '.h', '.cpp', '.cc', '.m', '.mm'} or 'target' in path.relative_to(root).parts:
            continue
        text = path.read_text()
        quoted_identity = any(re.search(r'["\']' + re.escape(name) + r'["\']', text) for name in identities)
        app_path = any(re.search(r'\bapp/' + re.escape(name) + r'/', text) for name in app_directories)
        static_entry = any(re.search(r'\b' + re.escape(name) + r'\b', text) for name in entry_symbols)
        cli_identity = any(re.search(r'--(?:app|app-package|bin|command)(?:=|\s+)' + re.escape(name) + r'(?:\s|$)', text)
                           for name in identities)
        if quoted_identity or app_path or static_entry or cli_identity:
            errors.append(f'{path.relative_to(root)}: platform code must not name a specific application')
    return errors


def inspect_dependency_capsule(root: Path) -> list[str]:
    """A small patched runtime is reviewable only with complete source identity."""
    capsule = root / 'vendor/microflow'
    if not capsule.exists():
        return []
    try:
        record = json.loads((root / 'upstream/microflow/source.json').read_text())
        expected = record['files']
        files = {p.relative_to(root).as_posix(): p for p in capsule.rglob('*') if p.is_file()}
        if set(expected) != set(files):
            return ['microflow: vendored source inventory differs from its reviewed ledger']
        errors = []
        for name, path in files.items():
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]:
                errors.append(f'{name}: vendored source hash differs from its reviewed ledger')
        return errors
    except (OSError, ValueError, KeyError, TypeError):
        return ['microflow: missing or malformed source provenance ledger']


def inspect_generated_imu_factory(root: Path) -> list[str]:
    """Keep profile-generated IMU data inside a fixed, typed HAL source seam."""
    hal = root / 'hal/imu'
    if not (hal / 'src/platform/nuttx').exists():
        return []
    errors = []
    relative = lambda path: path.relative_to(root).as_posix()
    nuttx = hal / 'src/platform/nuttx/mod.rs'
    build = hal / 'build.rs'
    emitter = hal / 'build/emit.rs'
    if not all(path.is_file() for path in (nuttx, build, emitter)):
        return ['hal/imu: generated factory must be emitted and included by the owning HAL package']

    source = re.sub(r'//[^\n]*|/\*.*?\*/', '', nuttx.read_text(), flags=re.S)
    include = 'include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));'
    if source.count(include) != 1 or re.search(r'include!\s*\(', source.replace(include, '')):
        errors.append(f'{relative(nuttx)}: include only the fixed imu_config.rs file from this HAL OUT_DIR')
    if not re.search(r'match\s+MODEL\s*\{\s*SensorModel::Bmi270\s*=>\s*Device::acquire\(BINDING,\s*config\)\??\s*,?\s*\}', source):
        errors.append(f'{relative(nuttx)}: generated model selection must map to its checked-in typed constructor')
    if not re.search(r'Session::start\(STACK_BYTES\s*,', source):
        errors.append(f'{relative(nuttx)}: generated execution budget must feed the checked-in factory')

    build_source = re.sub(r'//[^\n]*|/\*.*?\*/', '', build.read_text(), flags=re.S)
    if (not re.search(r'emit::write\s*\(\s*&out\.join\("imu_config\.rs"\)\s*,\s*binding\s*\)', build_source)
            or 'mod emit;' not in build_source
            or not re.search(r'profile\.binding\("imu"\)\s*==\s*"nuttx"\s*&&\s*os\s*==\s*"nuttx"', build_source)):
        errors.append(f'{relative(build)}: emit the fixed generated factory through the HAL-owned emitter')

    emitter_source = re.sub(r'//[^\n]*|/\*.*?\*/', '', emitter.read_text(), flags=re.S)
    # The emitter may escape profile strings and place parsed numeric fields, but
    # generated identifiers and constructor names must remain fixed Rust syntax.
    required = [
        r'let\s+model\s*=\s*match\s+binding\.model\s*\{\s*'
        r'nxrs_build_support::composition::ImuModel::Bmi270\s*=>\s*"SensorModel::Bmi270"\s*,?\s*\}',
        r'enum SensorModel\s*\{\{\s*Bmi270\s*\}\}',
        r'const MODEL:\s*SensorModel\s*=\s*\{model\}',
        r'const BINDING:\s*Binding<\'static>\s*=\s*Binding\s*\{\{',
        r'const STACK_BYTES:\s*usize\s*=\s*\{stack_bytes\}',
        r'path:\s*\{path:\?\}',
        r'address:\s*\{address\}',
        r'frequency:\s*\{frequency\}',
        r'irq_path:\s*\{irq_path\}',
    ]
    if any(not re.search(pattern, emitter_source) for pattern in required):
        errors.append(f'{relative(emitter)}: generated Rust must use the allowlisted typed model and escaped binding literals')
    if not (hal / 'src/sensors/bmi270.rs').is_file():
        errors.append('hal/imu: generated BMI270 selection has no checked-in sensor implementation')
    return errors


def inspect_hal_boundaries(root: Path) -> list[str]:
    """Reject platform selection in shared Rust and unreviewed native sources.

    The inventory permits native ABI glue only. Entries still require semantic
    code review; scalar declarations do not certify a mechanical boundary.
    """
    errors = inspect_generated_imu_factory(root)
    for path in (root / 'hal').glob('*/src/platform/mod.rs'):
        text = re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)
        exports = re.findall(r'\bpub(?:\([^)]*\))?\s+use\s+([^;]+);', text)
        # Check the selection boundary, not a mandatory wildcard spelling.
        # Exact exports prevent backend internals from accidentally reaching
        # public namespaces. No platform selector may wildcard-reexport.
        selected_export = r'selected::(?:[A-Za-z_]\w*|\{\s*[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*\s*,?\s*\})'
        if (not exports
                or any(not re.fullmatch(selected_export, export.strip()) for export in exports)
                or not re.search(r'\bmod\s+selected\s*;', text)
                or re.search(r'\b(?:fn|impl|struct|enum|trait)\b|\bmod\s+\w+\s*\{', text)):
            errors.append(f'{path.relative_to(root)}: select source modules and re-export selected items only; factories belong in the selected implementation')
    for path in (root / 'hal').glob('*/src/lib.rs'):
        source = re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)
        if re.search(r'\bpub\s+use\s+(?:api|platform)::\*\s*;', source):
            errors.append(f'{path.relative_to(root)}: HAL crate roots must name public exports explicitly')
    for path in (root / 'hal').glob('*/src/providers'):
        errors.append(f'{path.relative_to(root)}: HAL implementations belong in src/platform')
    for path in (root / 'hal').glob('*/src/platform/native*'):
        errors.append(f'{path.relative_to(root)}: name the HAL platform explicitly; native is ambiguous')
    for path in (root / 'hal').glob('*/src/platform/std*'):
        errors.append(f'{path.relative_to(root)}: std is an environment, not a platform; share ordinary Rust modules')
    if (root / 'platform/profiles/std.toml').exists():
        errors.append('platform/profiles/std.toml: std is not a platform')
    for path in (root / 'hal').glob('*/Cargo.toml'):
        data = tomllib.loads(path.read_text())
        if data.get('package', {}).get('metadata', {}).get('nxrs', {}).get('kind') == 'hal-capability':
            if 'std' in data['package']['metadata']['nxrs'].get('platforms', []):
                errors.append(f'{path.relative_to(root)}: HAL implementations must name actual operating systems')
    shared = {'api.rs', 'controller.rs', 'worker.rs', 'events.rs', 'record.rs',
              'udp.rs', 'file.rs', 'recording.rs', 'replay.rs', 'client.rs',
              'gatt_backend.rs', 'gatt_driver.rs', 'std.rs'}
    for path in (root / 'hal').rglob('*.rs'):
        relative = path.relative_to(root).as_posix()
        # SDK callbacks are ABI operations. These retired high-level symbols
        # instead connected Rust modules to Rust modules through a C protocol.
        text = re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)
        if re.search(r'\bfn\s+(?:(?:nxrs_)?host_wifi_\w+|(?:nxrs_)?host_ble_scan_\w+|'
                     r'(?:nxrs_)?ble_gatt_(?:open|submit|poll|recover|close)|'
                     r'(?:nxrs_)?ble_peer_lookup(?:_rust)?)\s*\(', text):
            errors.append(f'{relative}: internal HAL calls must use typed Rust, not a C protocol')
        if (path.name not in shared and '/shared/' not in relative
                and '/model/' not in relative
                and '/replay/' not in relative
                and '/recording/' not in relative
                and not relative.endswith('platform/gatt.rs')):
            continue
        # Comments may describe the rule without becoming platform selection.
        if (re.search(r'\b(?:target_(?:os|env|family|vendor)|hal_enabled|hal_mock|nxrs_hal_platform|nxrs_provider|NXRS_PLATFORM)\b', text)
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
    native_roots = [root / 'hal', root / 'driver']
    actual = {path.relative_to(root).as_posix(): path for native_root in native_roots for path in native_root.rglob('*')
              if path.is_file() and path.suffix in {'.c', '.h', '.m', '.mm', '.cc', '.cpp', '.hpp'}}
    for path in sorted(declared.keys() - actual.keys()):
        errors.append(f'{path}: stale native boundary entry')
    ordinary = r'\b(open|read|write|lseek|ftruncate|fsync|socket|send|recv|close|malloc|calloc|realloc|pthread_create|pthread_mutex_lock|pthread_mutex_trylock|clock_gettime|usleep|nanosleep|g_thread_try_new|g_usleep|g_get_monotonic_time)\s*\('
    for relative, path in actual.items():
        if not (re.fullmatch(r'hal/[^/]+/src/platform/(?:[^/]+/)+ffi/.+', relative)
                or re.fullmatch(r'driver/(?:[^/]+/)+src/platform/(?:[^/]+/)+ffi/.+', relative)
                or re.fullmatch(r'hal/[^/]+/build-support/src/ffi/.+', relative)
                or re.fullmatch(r'hal/tls/src/sdk/ffi/.+', relative)):
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
