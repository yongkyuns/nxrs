"""Scoped source provenance for the Zephyr comparison firmware."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _flags(variant):
    if variant is None:
        return {}
    if isinstance(variant, dict):
        return variant
    if hasattr(variant, 'native_defaults') or hasattr(variant, 'lean'):
        return {'native_defaults': bool(getattr(variant, 'native_defaults', False)),
                'lean': bool(getattr(variant, 'lean', False))}
    raise TypeError('variant must be a mapping or expose native_defaults/lean flags')


def firmware_input_names(mode, variant=None):
    """Return the explicit firmware inputs for a build mode and config variant."""
    if mode not in {'wire', 'packet', 'baseline'}:
        raise ValueError(f'unsupported firmware mode: {mode!r}')
    names = ['CMakeLists.txt', 'prj.conf', 'app.overlay', 'main.c', 'memory.c', 'memory.h']
    flags = _flags(variant)
    if flags.get('native_defaults'):
        names.append('native-defaults.conf')
    if flags.get('lean'):
        names.append('lean.conf')
    if mode != 'baseline':
        names += ['native_adapter.c', 'native_adapter.h', '../service-footprint/channel_scale_mq.c']
        if mode == 'packet':
            names += ['../service-footprint/pipeline_processing.c',
                      '../service-footprint/pipeline_processing.h',
                      '../service-footprint/payload_processing.c',
                      '../service-footprint/payload_processing.h']
    return tuple(names)


def _source_path(root: Path, name: str) -> Path:
    return (root / name).resolve()


def _load_legacy(directory):
    if directory is None:
        return {}, None
    path = Path(directory) / 'source-hashes.json'
    if not path.is_file():
        raise ValueError(f'missing frozen source hash manifest: {path}')
    import json
    contents = json.loads(path.read_text())
    if not isinstance(contents, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                             for k, v in contents.items()):
        raise ValueError(f'invalid legacy source hash manifest: {path}')
    if not contents:
        raise ValueError(f'empty frozen source hash manifest: {path}')
    return contents, _digest(path)


def _manifest_value(manifest, name):
    """Accept legacy keys relative to either the comparison dir or repo root."""
    candidates = (name, f'tests/zephyr-comparison/{name}',
                  name.removeprefix('../'))
    for candidate in candidates:
        if candidate in manifest:
            return manifest[candidate]
    # A few frozen maps use repository-relative names for shared service sources.
    if name.startswith('../'):
        candidate = f'tests/service-footprint/{Path(name).name}'
        if candidate in manifest:
            return manifest[candidate]
    return None


def verify_firmware_sources(directory, mode, variant=None, root=HERE):
    """Verify current firmware inputs against an optional frozen flat manifest.

    Returns current firmware digests, generated-control evidence, tooling digests,
    and legacy non-firmware entries as separate metadata groups.
    """
    root = Path(root).resolve()
    directory = Path(directory) if directory is not None else None
    names = firmware_input_names(mode, variant)
    firmware = {}
    for name in names:
        path = _source_path(root, name)
        if not path.is_file():
            raise ValueError(f'missing firmware input: {name}')
        firmware[name] = _digest(path)

    manifest, manifest_digest = _load_legacy(directory)
    missing = []
    changed = []
    for name, digest in firmware.items():
        old = _manifest_value(manifest, name)
        if old is None:
            # Current builds have no old manifest; an existing legacy manifest
            # must cover each input relevant to this variant.
            if manifest:
                missing.append(name)
        elif old != digest:
            changed.append({'name': name, 'legacy': old, 'current': digest})
    if missing or changed:
        details = []
        if missing:
            details.append('missing firmware inputs: ' + ', '.join(missing))
        if changed:
            details.append('modified firmware inputs: ' + ', '.join(item['name'] for item in changed))
        raise ValueError('; '.join(details))

    generated = None
    if mode != 'baseline':
        generator_path = root / 'generate_control.py'
        spec = importlib.util.spec_from_file_location('_zephyr_generate_control', generator_path)
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        source = _source_path(root, '../service-footprint/channel_scale_mq.c').read_text()
        adapted = generator.adapt(source).encode()
        generated = {'sha256': hashlib.sha256(adapted).hexdigest(),
                     'matches_frozen': None}
        frozen_candidates = ([Path(directory) / 'matched_control.c',
                              Path(directory) / 'zephyr' / 'matched_control.c']
                             if directory is not None else [])
        frozen = next((candidate for candidate in frozen_candidates if candidate.is_file()), None)
        if directory is not None and frozen is None:
            raise ValueError(f'missing frozen matched_control.c in {directory}')
        if frozen is not None:
            frozen_bytes = frozen.read_bytes()
            generated['frozen_sha256'] = hashlib.sha256(frozen_bytes).hexdigest()
            generated['matches_frozen'] = adapted == frozen_bytes
            if not generated['matches_frozen']:
                raise ValueError('generated matched_control.c differs from frozen control')

    historical = {}
    historical_tools = {}
    differences = []
    firmware_set = set(names)
    possible_inputs = set()
    variants = ({}, {'native_defaults': True}, {'lean': True},
                {'native_defaults': True, 'lean': True})
    for candidate_mode in ('wire', 'packet', 'baseline'):
        for candidate_variant in variants:
            possible_inputs.update(firmware_input_names(candidate_mode, candidate_variant))
    tool_names = {'build.py', 'generate_control.py', 'provenance.py',
                  'measure.py', 'report.py', 'run_matrix.py'}

    def current_for(name):
        if name.startswith('tests/zephyr-comparison/'):
            path = root / name.removeprefix('tests/zephyr-comparison/')
        elif name.startswith('tests/service-footprint/'):
            path = root.parent / 'service-footprint' / Path(name).name
        else:
            path = _source_path(root, name)
        return _digest(path) if path.is_file() else None

    for name, digest in manifest.items():
        normalized = name
        if normalized.startswith('tests/zephyr-comparison/'):
            normalized = normalized.removeprefix('tests/zephyr-comparison/')
        elif normalized.startswith('tests/service-footprint/'):
            normalized = '../service-footprint/' + Path(normalized).name
        if normalized in firmware_set:
            continue
        current = current_for(name)
        base = Path(name).name
        if base in tool_names:
            historical_tools[name] = digest
            category = 'tool'
        elif normalized in possible_inputs:
            historical[name] = digest
            category = 'input_not_used'
        elif base == 'speed.conf' and current is None:
            historical[name] = digest
            category = 'retired_non_input'
        else:
            historical[name] = digest
            category = 'historical_non_firmware'
        differences.append({'name': name, 'legacy_sha256': digest,
                            'current_sha256': current,
                            'change': ('missing' if current is None else
                                       'unchanged' if current == digest else 'changed'),
                            'classification': category})

    tools = {}
    for name in sorted(tool_names):
        path = root / name
        if path.is_file():
            tools[name] = _digest(path)
    for item in differences:
        if item['classification'] == 'tool':
            current = tools.get(Path(item['name']).name)
            item['current_sha256'] = current
            item['change'] = ('missing' if current is None else
                              'unchanged' if current == item['legacy_sha256'] else 'changed')
    return {
        'mode': mode,
        'variant': {key: bool(value) for key, value in _flags(variant).items()
                    if key in {'native_defaults', 'lean'}},
        'firmware_sources': firmware,
        'generated_control': generated,
        'historical_non_firmware': {
            'manifest_sha256': manifest_digest,
            'source_sha256': historical,
            'input_not_used_sha256': {
                item['name']: item['legacy_sha256'] for item in differences
                if item['classification'] == 'input_not_used'},
        },
        'current_tools': {'source_sha256': tools,
                          'legacy_source_sha256': historical_tools},
        'differences': differences,
    }
