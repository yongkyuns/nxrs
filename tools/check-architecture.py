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
    'app': {'service', 'api', 'hal', 'shared', 'devices'},
    'service': {'service', 'api', 'hal', 'shared'},
    'hal': {'api', 'hal', 'driver', 'support', 'shared'},
    'api': {'api'},
    'backend': {'api', 'driver', 'support'},
    'support': {'api'},
    'mock': {'api'},
    'driver': {'api', 'driver', 'shared'},
    'shared': {'shared'},
    'build-support': {'build-support'},
    'build': {'build-support'},
    'devices': {'hal', 'driver', 'shared'},
    'platform': {'service', 'api', 'backend', 'mock', 'driver', 'support', 'platform', 'shared'},
    'test': {'app', 'service', 'hal', 'api', 'backend', 'mock', 'driver', 'support', 'platform', 'devices', 'test', 'shared', 'build-support'},
}
PORTABLE = {'app', 'service', 'hal', 'api', 'shared', 'devices'}
PROVIDER_FEATURES = {'native', 'mock', 'nuttx', 'std', 'linux', 'macos', 'windows'}
# New third-party dependencies of portable production crates require a reviewed
# portability decision here. Do not infer portability merely from a crate name.
# cfg-if is a no_std, macro-only conditional expansion with no runtime dependency.
PORTABLE_EXTERNAL: set[str] = {'cfg-if'}
# Optional detector model packages: MicroFlow generates no_std quantized kernels,
# nalgebra has no std backend, and zune-jpeg is a single-threaded scalar decoder.
# Native PICO compute is private to the selected HAL implementation. Model/SDK
# provenance and qualification live in hal/detector/README.md; services do not
# receive a general third-party dependency exception.
PORTABLE_EXTERNAL_BY_PACKAGE = {
    # Embedded bus traits for shared acquisition timing observers.
    # Bus access and chip protocols belong to independent driver packages.
    'nxrs-acquisition': {'embedded-hal'},
    # Concrete resource construction uses the same portable bus traits.
    'nxrs-composition': {'embedded-hal'},
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
    if parts[0] == 'tools':
        return 'build' if parts == ('tools', 'build') else 'build-support'
    if parts == ('platform',):
        return 'devices'
    if parts[0] == 'hal':
        if parts == ('hal', 'common'):
            return 'api'
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
                if destination and ((role in {'app', 'service'} and roles[destination] in {'platform', 'build', 'build-support'})
                                    or (role in {'platform', 'build'} and roles[destination] == 'app')):
                    errors.append(f"{package['name']}: test dependencies cannot bypass the app/platform boundary")
                if (destination and role in {'hal', 'driver', 'build-support'}
                        and paths[destination] == Path('tools/build')):
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
                if (role in {'hal', 'driver', 'devices'} and dep.get('kind') == 'build'
                        and (destination == ('tools', 'build-support')
                             or (role == 'hal' and destination == ('tools', source[-1] + '-build-support')))):
                    continue
                if source == ('tools', 'build-support') and roles[target] == 'build-support':
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


def inspect_source_ownership(directory: Path, root: Path, owners: set[Path], build_dependencies: set[Path]) -> list[str]:
    """Rust source belongs to its Cargo package; test witnesses remain explicit."""
    errors = []
    manifest = directory / 'Cargo.toml'
    build = tomllib.loads(manifest.read_text()).get('package', {}).get('build')
    firmware = directory.relative_to(root).parts[0] in {'app', 'service', 'hal', 'driver', 'platform', 'shared'}
    if firmware and (directory / 'build.rs').exists():
        errors.append(f'{manifest.relative_to(root)}: build helpers belong under tools, not firmware')
    if isinstance(build, str):
        script = (directory / build).resolve()
        owner = next((parent for parent in script.parents if parent in owners), None)
        if firmware and (not script.is_relative_to(root / 'tools') or owner not in build_dependencies):
            errors.append(f'{manifest.relative_to(root)}: build scripts must belong to declared tooling build dependencies')
        elif not firmware and not script.is_relative_to(directory):
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
    for name in ['app', 'service', 'hal', 'driver', 'platform', 'tests', 'shared', 'tools']:
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
            build_dependencies = {Path(dep['path']).resolve() for dep in package['dependencies']
                                  if dep.get('kind') == 'build' and dep.get('path')}
            errors.extend(inspect_source_ownership(directory, root, owners, build_dependencies))
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
                identity = (r'\b(?:target_(?:os|arch|env|family|vendor)|device_binding|hal_enabled|hal_mock|hal_provider|nxrs_hal_platform|nxrs_provider|CARGO_CFG_PLATFORM|NXRS_PLATFORM|NXRS_RESOLVED_PROFILE)\b'
                            r'|(?:std|core)::(?:os|arch)::|std::env::consts::(?:OS|ARCH|FAMILY)|\blibc::'
                            r'|(?:#\s*\[\s*cfg(?:_attr)?\s*\(|\bcfg!\s*\()[^\];{}]*\b(?:unix|windows|platform)\b'
                            r'|["\']/(?:dev|proc)/|["\'](?:nuttx|linux|macos|windows)["\']')
                hidden_source = r'(?:#\[path|include!)[^;\n]*["\'][^"\']*(?:platform/|hal/[^/]+/src/platform/)'
                if re.search(identity, text) or re.search(hidden_source, text) or re.search(r'(?:::|\{)\s*construction\b', text):
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
        # HAL portability is an OS-independent API, not a no_std requirement.
        if role in {'api', 'shared'}:
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
    platform_sources = [root / name for name in
                        ('platform', 'tools/build', 'tools/build-support', 'tools/detector-build-support')]
    for path in (path for directory in platform_sources for path in directory.rglob('*')):
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


def declared_binding_catalogs(package: Path, stack=(), capability=None) -> list[Path]:
    """Follow only declared catalogs and named normal Cargo path dependencies."""
    package = package.resolve()
    if package in stack or len(stack) >= 64:
        raise ValueError('binding dependency cycle or depth limit')
    manifest = tomllib.loads((package / 'Cargo.toml').read_text())
    metadata = manifest.get('package', {}).get('metadata', {}).get('nxrs', {})
    if capability is not None:
        if (capability not in metadata.get('device-capabilities', [])
                or capability not in manifest.get('features', {})):
            raise ValueError('device binding requires a matching Cargo feature')
        dependencies = ((alias, dependency)
                        for target, table in manifest.get('target', {}).items()
                        if re.fullmatch(r'\s*cfg\s*\(\s*target_os\s*=\s*"nuttx"\s*\)\s*', target)
                        for alias, dependency in table.get('dependencies', {}).items())
        result = []
        for alias, dependency in dependencies:
            if not isinstance(dependency, dict) or not dependency.get('optional'):
                continue
            if 'package' not in dependency or 'path' not in dependency:
                continue
            source = (package / dependency['path']).resolve()
            driver = tomllib.loads((source / 'Cargo.toml').read_text())['package']
            if driver.get('metadata', {}).get('nxrs', {}).get('device', {}).get('capability') != capability:
                continue
            if driver['name'] != dependency['package']:
                raise ValueError(f'binding dependency {alias} package name mismatch')
            result.extend(declared_binding_catalogs(source, (*stack, package)))
        if not result:
            raise ValueError(f'declare an optional Cargo path dependency for {capability}')
        return result
    declarations = metadata.get('bindings', [])
    if not isinstance(declarations, list) or not declarations:
        raise ValueError('declare the package binding catalog inputs')
    result = []
    for declaration in declarations:
        if isinstance(declaration, str):
            if (not declaration or '\\' in declaration or ':' in declaration
                    or Path(declaration).is_absolute()
                    or any(part in {'', '.', '..'} for part in declaration.split('/'))):
                raise ValueError('binding catalog must be package-relative')
            path = package / declaration
            path.resolve().relative_to(package)
            result.append(path)
        elif isinstance(declaration, dict) and set(declaration) == {'dependency'}:
            alias = declaration['dependency']
            if not isinstance(alias, str):
                raise ValueError('binding import requires a dependency alias')
            dependencies = [table.get('dependencies', {}).get(alias)
                            for table in [manifest, *manifest.get('target', {}).values()]
                            if alias in table.get('dependencies', {})]
            if not dependencies or any(not isinstance(dep, dict) or 'path' not in dep for dep in dependencies):
                raise ValueError('binding import must name a normal Cargo path dependency')
            sources = {((package / dep['path']).resolve(), dep.get('package', alias)) for dep in dependencies}
            if len(sources) != 1:
                raise ValueError('ambiguous binding dependency')
            source, name = sources.pop()
            if tomllib.loads((source / 'Cargo.toml').read_text())['package']['name'] != name:
                raise ValueError('binding dependency package name mismatch')
            result.extend(declared_binding_catalogs(source, (*stack, package)))
        else:
            raise ValueError('invalid binding declaration')
    if len(set(result)) != len(result):
        raise ValueError('duplicate binding catalog')
    return result


def inspect_generated_imu_factory(root: Path) -> list[str]:
    """Check declarative sensor generation and the current typed runtime seam."""
    root = root.resolve()
    errors = []
    relative = lambda path: path.relative_to(root).as_posix()
    for capability in ('imu', 'gnss'):
        hal = root / 'hal' / capability
        owner = root / 'platform'
        nuttx = owner / 'src/nuttx' / f'{capability}.rs'
        if not nuttx.is_file():
            continue
        manifest = owner / 'Cargo.toml'
        if not manifest.is_file():
            errors.append(f'hal/{capability}: declare configuration in the owning package and use shared preparation')
            continue
        data = tomllib.loads(manifest.read_text())
        build = (owner / data.get('package', {}).get('build', 'build.rs')).resolve()
        if build != (root / 'tools/build-support/cargo/composition.rs').resolve() or not build.is_file():
            errors.append(f'{relative(manifest)}: use the generic shared-preparation tooling adapter')
            continue
        build_source = re.sub(r'//[^\n]*|/\*.*?\*/', '', build.read_text(), flags=re.S)
        calls = re.findall(r'\b([A-Za-z_]\w*(?:::\w+)*)\s*\(', build_source)
        macros = re.findall(r'\b(\w+)!\s*[\(\{\[]', build_source)
        if ('nxrs_build_support::configure_composition' not in calls
                or any(call not in {'main', 'nxrs_build_support::configure_composition',
                                    'unwrap', 'unwrap_or_else', 'expect'} for call in calls)
                or any(macro != 'panic' for macro in macros)
                or re.search(r'\b(?:mod|include|format|configure_composition_with)\b', build_source)):
            errors.append(f'{relative(build)}: sensor build scripts must be generic shared-preparation adapters')
        if any((owner / 'build').rglob('*.rs')):
            errors.append(f'hal/{capability}/build: host generation belongs in shared build support')

        try:
            metadata = tomllib.loads(manifest.read_text())['package']['metadata']['nxrs']
            if 'configuration' in metadata:
                raise ValueError('use resource bindings, not a Rust projection schema')
            implementations = set()
            for catalog_path in declared_binding_catalogs(owner, capability=capability):
                catalog = tomllib.loads(catalog_path.read_text())
                if catalog.get('version') != 2 or not catalog.get('binding'):
                    raise ValueError('declare version 2 resource bindings')
                for entry in catalog['binding']:
                    identifier = entry['implementation']
                    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Z][A-Za-z0-9]*', identifier) or identifier == 'Self':
                        raise ValueError('implementation must name one checked-in Rust type')
                    implementations.add(identifier)
            output = f'{capability}_config.rs'
        except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
            errors.append(f'{relative(manifest)}: invalid declarative sensor configuration: {error}')
            continue

        source = re.sub(r'//[^\n]*|/\*.*?\*/', '', nuttx.read_text(), flags=re.S)
        include = f'include!(concat!(env!("OUT_DIR"), "/{output}"));'
        if source.count(include) != 1 or re.search(r'include!\s*\(', source.replace(include, '')):
            errors.append(f'{relative(nuttx)}: include only the declared generated output from its owning OUT_DIR')

        if (re.search(r'\bmod\s+implementations\s*\{', source)
                or any(re.search(r'\b' + re.escape(name) + r'\b', source) for name in implementations)):
            errors.append(f'{relative(nuttx)}: generate driver types from declared Cargo bindings')
        if re.search(r'\b(?:MODEL|SensorModel)\b', source):
            errors.append(f'{relative(nuttx)}: select the catalog implementation directly, without a model table')
        if capability == 'imu':
            compact = re.sub(r'\s+', '', source)
            if ('macro_rules!declare_device' not in compact
                    or 'static$name:Instance<' not in compact
                    or '$open($binding,trace)' not in compact
                    or '<$sensor>::from_interface(' not in compact):
                errors.append(f'{relative(nuttx)}: use canonical declarations and the checked-in typed resource factory')
            instance = hal / 'src/platform/nuttx/instance.rs'
            runtime = re.sub(r'\s+', '', instance.read_text()) if instance.is_file() else ''
            if ('state:DeviceState' not in runtime or 'storage:Mutex<Option<device::Device<S>>>' not in runtime
                    or 'Worker::start_guarded("imu-acquire",self.stack_bytes,' not in runtime
                    or 'device::Device::open(config,self.irq_path,trace,&self.state,' not in runtime):
                errors.append(f'{relative(instance)}: retain per-device storage and ownership through guarded worker retirement')
            for path in (nuttx, instance, instance.parent / 'device.rs'):
                if path.is_file() and re.search(r'\bstatic\s+OWNED\b', path.read_text()):
                    errors.append(f'{relative(path)}: global device ownership must not replace canonical instance state')
            hal_manifest = hal / 'Cargo.toml'
            if hal_manifest.is_file():
                data = tomllib.loads(hal_manifest.read_text())
                if data.get('package', {}).get('metadata', {}).get('nxrs', {}).get('bindings'):
                    errors.append(f'{relative(hal_manifest)}: concrete bindings belong to the platform device catalogue')
                for table in [data, *data.get('target', {}).values()]:
                    for alias, dependency in table.get('dependencies', {}).items():
                        if isinstance(dependency, dict) and dependency.get('path'):
                            package = (hal / dependency['path']).resolve()
                            manifest = package / 'Cargo.toml'
                            if manifest.is_file() and tomllib.loads(manifest.read_text()).get('package', {}).get('metadata', {}).get('nxrs', {}).get('bindings'):
                                errors.append(f'{relative(hal_manifest)}: IMU must consume the normalized contract, not concrete sensor {alias}')
            for path in (hal / 'src').rglob('*.rs'):
                if path.name.endswith('_tests.rs'):
                    continue
                if any(re.search(r'\b' + re.escape(name) + r'\b', path.read_text()) for name in implementations):
                    errors.append(f'{relative(path)}: concrete sensor wiring belongs in the platform device catalogue')
        else:
            runtime = (hal / 'src/platform/nuttx/mod.rs').read_text()
            compact = re.sub(r'\s+', '', source)
            if ('static$name:Instance<' not in compact or '$stack' not in compact
                    or 'Worker::start_guarded' not in runtime
                    or 'storage: Mutex<Option<Active<R>>>' not in runtime
                    or re.search(r'\bstatic\s+OWNED\b', runtime)):
                errors.append(f'{relative(nuttx)}: GNSS must retain canonical state through guarded retirement')
            manifest_data = tomllib.loads((hal / 'Cargo.toml').read_text())
            for table in [manifest_data, *manifest_data.get('target', {}).values()]:
                if 'nxrs-ubx-driver' in table.get('dependencies', {}):
                    errors.append('hal/gnss: concrete receiver construction belongs in the platform catalogue')

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
        modules = set(re.findall(r'\bmod\s+(\w+)\s*;', text))
        aliases = {f'{name} as selected' for name in modules if name != 'selected'}
        crate_source = re.sub(r'//[^\n]*|/\*.*?\*/', '', (path.parent.parent / 'lib.rs').read_text(), flags=re.S)
        crate_modules = set(re.findall(r'\bmod\s+(\w+)\s*;', crate_source))
        aliases.update(f'crate::{name} as selected' for name in crate_modules)
        selected_aliases = re.findall(r'\buse\s+((?:crate::)?\w+\s+as\s+selected)\s*;', text)
        # Check the selection boundary, not a mandatory wildcard spelling.
        # Exact exports prevent backend internals from accidentally reaching
        # public namespaces. No platform selector may wildcard-reexport.
        selected_export = r'selected::(?:[A-Za-z_]\w*|\{\s*[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*\s*,?\s*\})'
        if (not exports
                or any(not (re.fullmatch(selected_export, export.strip()) or export.strip() == 'selected as construction' or export.strip() in aliases) for export in exports)
                or any(alias not in aliases for alias in selected_aliases)
                or not ('selected' in modules or any(alias in aliases for alias in selected_aliases))
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
    if (root / 'platform/std/profile.toml').exists():
        errors.append('platform/std/profile.toml: std is not a platform')
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
        if re.search(r'\b(?:hal_enabled|hal_mock|hal_provider)\b', text):
            errors.append(f'{relative}: use the resolved platform selector')
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
        if (re.search(r'\b(?:target_(?:os|env|family|vendor)|hal_enabled|hal_mock|hal_provider|nxrs_hal_platform|nxrs_provider|CARGO_CFG_PLATFORM|NXRS_PLATFORM|NXRS_RESOLVED_PROFILE)\b', text)
                or re.search(r'(?:#\s*\[\s*cfg(?:_attr)?\s*\(|\bcfg!\s*\()[^\];{}]*\b(?:unix|windows|platform)\b', text)):
            errors.append(f'{relative}: shared Rust behavior must not select a platform/provider')
    inventory = root / 'hal/ffi-boundaries.toml'
    entries = tomllib.loads(inventory.read_text()).get('bridge', []) if inventory.is_file() else []
    declared = {}
    for entry in entries:
        path = entry.get('path', '')
        if path in declared:
            errors.append(f'{path}: duplicate native boundary entry')
        declared[path] = entry
    native_roots = [root / 'hal', root / 'driver', root / 'tools']
    actual = {path.relative_to(root).as_posix(): path for native_root in native_roots for path in native_root.rglob('*')
              if path.is_file() and path.suffix in {'.c', '.h', '.m', '.mm', '.cc', '.cpp', '.hpp'}}
    for path in sorted(declared.keys() - actual.keys()):
        errors.append(f'{path}: stale native boundary entry')
    ordinary = r'\b(open|read|write|lseek|ftruncate|fsync|socket|send|recv|close|malloc|calloc|realloc|pthread_create|pthread_mutex_lock|pthread_mutex_trylock|clock_gettime|usleep|nanosleep|g_thread_try_new|g_usleep|g_get_monotonic_time)\s*\('
    for relative, path in actual.items():
        if not (re.fullmatch(r'hal/[^/]+/src/platform/(?:[^/]+/)+ffi/.+', relative)
                or re.fullmatch(r'driver/(?:[^/]+/)+src/platform/(?:[^/]+/)+ffi/.+', relative)
                or re.fullmatch(r'tools/[^/]+/src/ffi/.+', relative)
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
