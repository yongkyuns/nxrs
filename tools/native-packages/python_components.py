#!/usr/bin/env python3
"""Observe bounded system-Python distribution metadata; never waive advisories.

Run in the guarded hosted image with /usr/bin/python3 -I. This is metadata
coverage of admitted startup directories, not proof of package contents,
import resolution, module absence or the complete deployed Python environment.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
from email import policy
from email.parser import BytesHeaderParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

STARTUP_PATHS = tuple(sys.path)
# -I omits the script directory. Only these checked-out audit helpers are added.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate

u = candidate.ubuntu
ROOT = u.ROOT
SCOPE = 'hosted-isolated-system-python-distribution-metadata'
INPUTS = ('tools/native-packages/python_components.py',
          'tests/host/test_python_components.py', 'tools/native-packages/ubuntu.py',
          '.github/workflows/minimized-native-packages.yml', 'tools/native-packages/README.md')
MAX_ENTRIES = 4096
MAX_DISTRIBUTIONS = 512
MAX_METADATA = 256 * 1024
MAX_RECORD = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
FALSE_FLAGS = ('qualified', 'package_contents_authenticated', 'import_resolution_qualified',
               'component_absence_proved', 'complete_python_environment_qualified',
               'full_native_advisory_closure', 'physical_qualified', 'fips_validated',
               'deployment_permitted')
require = u.require


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def public_path(value):
    require(type(value) is str and len(value) <= 512
            and re.fullmatch(r'/usr/(?:local/)?lib/[A-Za-z0-9_.+!~/-]+', value)
            and PurePosixPath(value).as_posix() == value
            and '..' not in PurePosixPath(value).parts, 'system-python-search-path')
    return Path(value)


def protected(path):
    for part in (path, *path.parents):
        info = part.lstat()
        require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0
                and not info.st_mode & 0o022, 'protected-system-python-metadata-path')


def search_paths(values):
    require(type(values) is tuple and 1 <= len(values) <= 16, 'system-python-search-path-count')
    paths = [public_path(value) for value in values]
    require(len(set(paths)) == len(paths), 'unique-system-python-search-paths')
    result = []
    for path in paths:
        if not os.path.lexists(path):
            # CPython includes this conventional zip path even when absent.
            require(path == Path('/usr/lib/python312.zip'), 'existing-system-python-directory')
            result.append(dict(path=str(path), kind='absent-default-zip'))
            continue
        protected(path)
        require(path.is_dir(), 'filesystem-system-python-directory')
        result.append(dict(path=str(path), kind='directory'))
    require(any(row['kind'] == 'directory' for row in result), 'system-python-metadata-directories')
    return result


def stable_file(name, limit, *, directory=None):
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                 dir_fd=directory)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= limit,
                'bounded-regular-python-metadata')
        data = bytearray()
        while True:
            chunk = os.read(fd, min(65536, before.st_size - len(data) + 1))
            if not chunk:
                break
            data.extend(chunk)
            require(len(data) <= before.st_size, 'stable-python-metadata-size')
        require(len(data) == before.st_size and identity(os.fstat(fd)) == identity(before)
                and identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) == identity(before),
                'stable-python-metadata')
        return bytes(data)
    finally:
        os.close(fd)


def metadata_fields(raw):
    try:
        value = BytesHeaderParser(policy=policy.default.clone(raise_on_defect=True)).parsebytes(raw)
        require(not value.defects, 'python-metadata-header')
        fields = {}
        for name in ('Name', 'Version'):
            values = value.get_all(name, [])
            require(len(values) == 1 and isinstance(values[0], str),
                    'unique-python-metadata-identity')
            fields[name] = str(values[0])
    except (ValueError, TypeError):
        raise u.Refusal('python-metadata-header') from None
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', fields['Name'])
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+!~-]{0,127}', fields['Version']),
            'public-python-package-identity')
    return re.sub(r'[-_.]+', '-', fields['Name'].lower()), fields['Version']


def observation(raw):
    return dict(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))


def metadata_failure(raw, path, reason):
    """Project one refused, unchanged system file without exposing header text."""
    try:
        public_path(str(path))
        protected(path)
        require(type(raw) is bytes and 0 < len(raw) <= MAX_METADATA,
                'bounded-python-metadata-diagnostic')
        require(stable_file(path, MAX_METADATA) == raw, 'unchanged-python-metadata-diagnostic')
    except (OSError, u.Refusal):
        return
    # The lenient parse is for diagnostics only. metadata_fields() remains the
    # strict admission predicate, and the caller always raises its refusal.
    details = dict(parser_failed=True, name_fields=None, version_fields=None, defect_types=[])
    known = {'MissingHeaderBodySeparatorDefect', 'FirstHeaderLineIsContinuationDefect',
             'MisplacedEnvelopeHeaderDefect', 'MalformedHeaderDefect'}
    try:
        value = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        headers = list(value.raw_items())
        details = dict(parser_failed=False,
            name_fields=sum(key.lower() == 'name' for key, _ in headers),
            version_fields=sum(key.lower() == 'version' for key, _ in headers),
            defect_types=sorted({type(d).__name__ if type(d).__name__ in known else 'other'
                                 for d in value.defects}))
    except (ValueError, TypeError):
        pass
    reasons = {'python-metadata-header', 'unique-python-metadata-identity',
               'public-python-package-identity'}
    value = dict(schema=1, scope='system-python-metadata-refusal', qualified=False, path=str(path),
        reason=reason if reason in reasons else 'python-metadata-refused',
        **observation(raw), **details)
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
    require(len(encoded.encode()) <= 2048, 'python-metadata-diagnostic-budget')
    print('PYTHON_METADATA_REFUSAL ' + encoded, flush=True)


def scan(paths):
    """Filesystem-only helper, also exercised with owned synthetic metadata."""
    distributions = []
    metadata_paths = set()
    total = 0
    for path in paths:
        root = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            before = os.fstat(root)
            names = []
            with os.scandir(root) as entries:
                for entry in entries:
                    names.append(entry.name)
                    require(len(names) <= MAX_ENTRIES, 'python-directory-entry-budget')
            for name in sorted(names):
                if not name.endswith(('.dist-info', '.egg-info')):
                    continue
                require(re.fullmatch(r'[A-Za-z0-9_.+!~-]+', name), 'python-metadata-entry-name')
                info = os.stat(name, dir_fd=root, follow_symlinks=False)
                record = None
                if name.endswith('.egg-info') and stat.S_ISREG(info.st_mode):
                    raw = stable_file(name, MAX_METADATA, directory=root)
                    metadata_path = path / name
                else:
                    require(stat.S_ISDIR(info.st_mode), 'python-metadata-directory')
                    directory = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=root)
                    try:
                        start = os.fstat(directory)
                        leaf = 'METADATA' if name.endswith('.dist-info') else 'PKG-INFO'
                        raw = stable_file(leaf, MAX_METADATA, directory=directory)
                        metadata_path = path / name / leaf
                        if name.endswith('.dist-info'):
                            try:
                                os.stat('RECORD', dir_fd=directory, follow_symlinks=False)
                            except FileNotFoundError:
                                pass
                            else:
                                data = stable_file('RECORD', MAX_RECORD, directory=directory)
                                total += len(data)
                                record = dict(path=str(path / name / 'RECORD'), **observation(data))
                        require(identity(os.fstat(directory)) == identity(start)
                                and identity(os.stat(name, dir_fd=root, follow_symlinks=False)) == identity(start),
                                'stable-python-metadata-directory')
                    finally:
                        os.close(directory)
                total += len(raw)
                require(total <= MAX_TOTAL, 'python-metadata-total-budget')
                try:
                    package, version = metadata_fields(raw)
                except u.Refusal as error:
                    metadata_failure(raw, metadata_path, str(error))
                    raise
                require(len(distributions) < MAX_DISTRIBUTIONS, 'python-distribution-count-budget')
                require(metadata_path not in metadata_paths, 'unique-python-metadata-path')
                metadata_paths.add(metadata_path)
                distributions.append(dict(name=package, version=version,
                    metadata=dict(path=str(metadata_path), **observation(raw)), record=record))
            require(identity(os.fstat(root)) == identity(before)
                    and identity(path.lstat()) == identity(before), 'stable-python-search-directory')
        finally:
            os.close(root)
    counts = Counter(row['name'] for row in distributions)
    return dict(distributions=sorted(distributions, key=lambda row: (row['name'], row['metadata']['path'])),
                duplicate_names=sorted(name for name, count in counts.items() if count > 1))


def source_inputs():
    return {name: u.file_digest(ROOT / name) for name in INPUTS}


def join_python(parent, executable, image):
    selected = parent['inventory']['roots']['python']
    require(selected['origin'] == 'ubuntu' and selected['image'] == str(executable)
            and json.dumps({key: selected[key] for key in ('sha256', 'size', 'elf')}, sort_keys=True)
            == json.dumps(image, sort_keys=True),
            'same-candidate-python-image')


def run(candidate_path, report):
    u.hosted()  # Before metadata inspection, filesystem writes or native commands.
    require(not os.path.lexists(report), 'new-system-python-component-report')
    require(sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 12)
            and sys.prefix == sys.base_prefix == '/usr' and sys.flags.isolated == 1
            and sys.flags.ignore_environment == 1 and sys.flags.no_user_site == 1,
            'isolated-system-python-required')
    executable = Path(sys.executable).resolve()
    require(executable == Path('/usr/bin/python3.12'), 'system-python-executable')
    raw, parent = candidate.read(candidate_path)
    head = u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip()
    before = source_inputs()
    require(parent['checkout'] == dict(clean=True, commit=head)
            and parent['inputs'] == candidate.source_inputs()
            and parent['advisory_status'] == 'complete'
            and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
            'current-system-python-candidate')
    candidate.validate_advisory_result(parent, head)
    image = u.image_identity(executable)
    join_python(parent, executable, image)
    admitted = search_paths(STARTUP_PATHS)
    directories = [Path(row['path']) for row in admitted if row['kind'] == 'directory']
    value = scan(directories)
    for row in value['distributions']:
        for field in ('metadata', 'record'):
            if row[field] is not None:
                public_path(row[field]['path'])
                protected(Path(row[field]['path']))
    require(value == scan(directories) and admitted == search_paths(STARTUP_PATHS)
            and image == u.image_identity(executable) and before == source_inputs()
            and candidate.read(candidate_path)[0] == raw and parent['inputs'] == candidate.source_inputs()
            and u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == head
            and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
            'unchanged-system-python-component-inputs')
    result = dict(schema=2, scope=SCOPE, collection_status='complete', checkout=dict(clean=True, commit=head),
                  inputs=before, candidate_report_sha256=u.digest(raw), python_image=image,
                  startup_search_paths=admitted, isolated=True, **value,
                  advisory_findings_cleared=0, checked_on=datetime.now(timezone.utc).isoformat(),
                  **dict.fromkeys(FALSE_FLAGS, False))
    encoded = (json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n').encode()
    require(len(encoded) <= 1024 * 1024, 'system-python-public-report-budget')
    with os.fdopen(os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
        out.write(encoded)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.candidate, args.report)
    except Exception as error:
        category = str(error) if isinstance(error, u.Refusal) and re.fullmatch(r'[a-z0-9-]{1,100}', str(error)) else type(error).__name__
        parser.exit(1, 'System Python component collection refused (' + category + ').\n')
    print('System Python distribution metadata observed; advisory and component-absence gates unchanged.')


if __name__ == '__main__':
    main()
