#!/usr/bin/env python3
"""Observe protected system-Python startup-directory payloads without importing them.

This filesystem inventory supplements the separate distribution metadata receipt.
It neither authenticates package contents nor proves dynamic import closure,
component absence or advisory applicability. No payload bytes are exported.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

STARTUP_PATHS = tuple(sys.path)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import python_components as components

u = components.u
ROOT = u.ROOT
SCOPE = 'hosted-isolated-system-python-startup-payload-inventory'
INPUTS = tuple(sorted(set(components.INPUTS) | {
    'tools/native-packages/python_payloads.py', 'tests/host/test_python_payloads.py'}))
MAX_ENTRIES = 65536
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_DEPTH = 32
MAX_REPORT = 16 * 1024 * 1024
FALSE_FLAGS = components.FALSE_FLAGS + ('dynamic_importer_closure_qualified',)
require = u.require


def normal(info, owner, kind):
    require(info.st_uid == owner and (kind == 'link' or not info.st_mode & 0o022),
            'protected-python-payload-owner-mode')


def entry_name(name):
    require(type(name) is str and 0 < len(name.encode('utf-8')) <= 255
            and name not in ('.', '..') and not any(c in name for c in '/\\\0\r\n'),
            'python-payload-entry-name')


def inside(path, roots):
    return any(path == root or path.is_relative_to(root) for root in roots)


def roots_for(paths):
    require(type(paths) is list and 1 <= len(paths) <= 16
            and all(isinstance(path, Path) for path in paths),
            'python-payload-root-set')
    require(len(set(paths)) == len(paths) and all(path.is_absolute() for path in paths),
            'canonical-python-payload-roots')
    # An independently admitted nested startup directory is already covered by
    # its enclosing root. Preserve the original search order in the outer receipt.
    return sorted(path for path in paths if not any(
        other != path and path.is_relative_to(other) for other in paths))


def scan(paths, *, owner=0, deadline=None):
    """Read owned fixture trees in tests; production supplies only admitted roots."""
    roots = roots_for(paths)
    deadline = time.monotonic() + 120 if deadline is None else deadline
    entries = {}; total = 0

    def budget():
        require(time.monotonic() < deadline, 'python-payload-deadline')

    def record(path, value):
        key = str(path)
        require(len(key.encode('utf-8')) <= 1024 and key not in entries
                and len(entries) < MAX_ENTRIES, 'python-payload-inventory-budget')
        entries[key] = value

    def regular(parent, name, path, expected):
        nonlocal total
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and components.identity(before) == components.identity(expected)
                    and 0 <= before.st_size <= MAX_FILE, 'bounded-regular-python-payload')
            normal(before, owner, 'file')
            require(total + before.st_size <= MAX_TOTAL, 'python-payload-total-budget')
            count = 0; digest = hashlib.sha256()
            while True:
                budget()
                chunk = os.read(fd, min(65536, before.st_size - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                require(count <= before.st_size, 'stable-python-payload-size')
                digest.update(chunk)
            require(count == before.st_size and components.identity(before) == components.identity(os.fstat(fd))
                    == components.identity(os.stat(name, dir_fd=parent, follow_symlinks=False)),
                    'stable-python-payload-file')
            total += count
            record(path, dict(kind='file', sha256=digest.hexdigest(), size=count,
                              mode=stat.S_IMODE(before.st_mode)))
        finally:
            os.close(fd)

    def directory(fd, path, expected, depth):
        budget(); require(depth <= MAX_DEPTH, 'python-payload-depth-budget')
        before = os.fstat(fd)
        require(stat.S_ISDIR(before.st_mode) and components.identity(before) == components.identity(expected),
                'stable-python-payload-directory')
        normal(before, owner, 'directory')
        record(path, dict(kind='directory', mode=stat.S_IMODE(before.st_mode)))
        names = []
        with os.scandir(fd) as children:
            for child in children:
                entry_name(child.name); names.append(child.name)
                require(len(names) <= MAX_ENTRIES, 'python-payload-directory-budget')
        for name in sorted(names):
            budget(); child_path = path / name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:directory(child, child_path, info, depth + 1)
                finally:os.close(child)
            elif stat.S_ISREG(info.st_mode):
                regular(fd, name, child_path, info)
            elif stat.S_ISLNK(info.st_mode):
                normal(info, owner, 'link')
                target = os.readlink(name, dir_fd=fd)
                target_bytes = os.fsencode(target)
                require(0 < len(target_bytes) <= 512 and not any(c in target for c in '\0\r\n'),
                        'bounded-python-payload-link')
                resolved = child_path.resolve(strict=True)
                require(inside(resolved, roots), 'python-payload-link-escape')
                require(components.identity(info) == components.identity(
                    os.stat(name, dir_fd=fd, follow_symlinks=False)), 'stable-python-payload-link')
                # Link text can contain a private intermediate path even when
                # its final resolution returns to an admitted public directory.
                record(child_path, dict(kind='link', target_sha256=hashlib.sha256(target_bytes).hexdigest(),
                                        target_bytes=len(target_bytes), resolved=str(resolved)))
            else:
                raise u.Refusal('regular-python-payload-entry-required')
        require(components.identity(before) == components.identity(os.fstat(fd))
                == components.identity(path.lstat()), 'stable-python-payload-directory')

    for path in roots:
        require(path == path.resolve(strict=True), 'canonical-python-payload-root')
        expected = path.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:directory(fd, path, expected, 0)
        finally:os.close(fd)
    for value in entries.values():
        if value['kind'] == 'link':
            require(value['resolved'] in entries and entries[value['resolved']]['kind'] in ('file', 'directory'),
                    'inventoried-python-payload-link-target')
    return dict(entries=entries, payload_bytes=total, roots=[str(path) for path in roots])


def source_inputs():
    return {name: u.file_digest(ROOT / name) for name in INPUTS}


def run(candidate_path, metadata_path, report):
    u.hosted()  # Before source/payload reads, native commands or filesystem writes.
    require(not os.path.lexists(report), 'new-system-python-payload-report')
    require(sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 12)
            and sys.prefix == sys.base_prefix == '/usr' and sys.flags.isolated == 1
            and sys.flags.ignore_environment == 1 and sys.flags.no_user_site == 1,
            'isolated-system-python-required')
    executable = Path(sys.executable).resolve()
    require(executable == Path('/usr/bin/python3.12'), 'system-python-executable')
    raw, parent = components.candidate.read(candidate_path)
    metadata_raw, metadata = components.candidate.read(metadata_path)
    head = u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip()
    before = source_inputs()
    require(parent['checkout'] == metadata['checkout'] == dict(clean=True, commit=head)
            and parent['inputs'] == components.candidate.source_inputs()
            and parent['advisory_status'] == 'complete'
            and type(metadata['schema']) is int and metadata['schema'] == 3 and metadata['scope'] == components.SCOPE
            and metadata['collection_status'] == 'complete'
            and metadata['isolated'] is True
            and metadata['candidate_report_sha256'] == u.digest(raw)
            and metadata['inputs'] == components.source_inputs()
            and all(metadata[k] is False for k in components.FALSE_FLAGS)
            and type(metadata['advisory_findings_cleared']) is int and metadata['advisory_findings_cleared'] == 0
            and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
            'current-system-python-payload-parents')
    components.candidate.validate_advisory_result(parent, head)
    image = u.image_identity(executable); components.join_python(parent, executable, image)
    require(metadata['python_image'] == image, 'same-metadata-python-image')
    admitted = components.search_paths(STARTUP_PATHS)
    require(metadata['startup_search_paths'] == admitted, 'same-python-startup-directories')
    directories = [Path(row['path']) for row in admitted if row['kind'] == 'directory']
    observed = components.scan(directories)
    require(all(metadata[k] == observed[k] for k in ('distributions', 'duplicate_names')),
            'same-python-distribution-metadata')
    deadline = time.monotonic() + 120
    value = scan(directories, deadline=deadline)
    for distribution in observed['distributions']:
        for field in ('metadata', 'record'):
            item = distribution[field]
            if item is not None:
                actual = value['entries'].get(item['path'], {})
                require(actual.get('kind') == 'file' and all(actual.get(k) == item[k] for k in ('sha256', 'size')),
                        'same-python-metadata-payload-bytes')
    require(value == scan(directories, deadline=deadline) and observed == components.scan(directories)
            and admitted == components.search_paths(STARTUP_PATHS)
            and image == u.image_identity(executable) and before == source_inputs()
            and components.candidate.read(candidate_path)[0] == raw
            and components.candidate.read(metadata_path)[0] == metadata_raw
            and parent['inputs'] == components.candidate.source_inputs()
            and u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == head
            and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
            'unchanged-system-python-payload-inputs')
    result = dict(schema=1, scope=SCOPE, collection_status='complete', checkout=dict(clean=True, commit=head),
                  inputs=before, candidate_report_sha256=u.digest(raw), metadata_report_sha256=u.digest(metadata_raw),
                  python_image=image, startup_search_paths=admitted, isolated=True, protected_owner_uid=0,
                  **value, advisory_findings_cleared=0, checked_on=datetime.now(timezone.utc).isoformat(),
                  **dict.fromkeys(FALSE_FLAGS, False))
    encoded = (json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n').encode()
    require(len(encoded) <= MAX_REPORT, 'system-python-payload-public-report-budget')
    with os.fdopen(os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
        output.write(encoded)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'metadata', 'report'):parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    try:run(args.candidate, args.metadata, args.report)
    except Exception as error:
        category = str(error) if isinstance(error, u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}', str(error)) else type(error).__name__
        parser.exit(1, 'System Python payload collection refused (' + category + ').\n')
    print('System Python payloads observed; authentication, import closure and advisory gates remain open.')


if __name__ == '__main__':main()
