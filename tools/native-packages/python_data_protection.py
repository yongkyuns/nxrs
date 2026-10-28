#!/usr/bin/env python3
"""Observe CI image permissions or protect the reviewed Python data layout.

The read-only inventory guard stays unchanged. This setup operation authenticates
no existing contents and grants no package, advisory or release qualification.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import python_components as components

u = components.u
candidate = components.candidate
ROOT = u.ROOT
TARGET = Path('/usr/share/python-babel-localedata/locale-data')
DIRECTORIES = (Path('/usr/share'), TARGET.parent, TARGET, Path('/usr/share/ieee-data'))
FIXED_FILES = (TARGET.parent / 'global.dat', Path('/usr/share/ieee-data/iab.txt'),
               Path('/usr/share/ieee-data/oui.txt'))
# Basename layout only, independently inspected in the public Ubuntu
# python-babel-localedata_2.10.3-3build1_all.deb archive (SHA-256
# 2dd9e1ee14f4e19185e4ca6a97476ab3f1e64aa1bd08851c515a24f7a74cefdc).
# This authenticates neither installed contents nor the hosted package state.
LOCALE_FILE_COUNT = 803
LOCALE_NAMES_SHA256 = '354ce73fdda4251c1a585233ffc640d77ea8565c5e638ac9e1e8482d034e1854'
FILE_BATCH_SIZE = 64
OBSERVATION_PHASES = ('before-tests', 'after-tests', 'after-install',
                      'after-refresh', 'before-python-observers')
# Public system names only. Never enumerate directories, read contents or
# resolve/read symlink targets. This is observation, not permission admission.
OBSERVATION_PATHS = tuple(map(Path, (
    '/', '/usr', '/usr/share', '/usr/share/python-babel-localedata',
    '/usr/share/python-babel-localedata/locale-data',
    '/usr/share/python-babel-localedata/global.dat', '/usr/share/ieee-data',
    '/usr/share/ieee-data/iab.txt', '/usr/share/ieee-data/oui.txt',
    '/usr/share/keyrings', '/usr/share/keyrings/ubuntu-archive-keyring.gpg')))
INPUTS = ('tools/native-packages/python_data_protection.py',
          'tests/host/test_python_data_protection.py', *components.INPUTS)
FALSE_FLAGS = ('qualified', 'package_contents_authenticated', 'import_resolution_qualified',
               'full_native_advisory_closure', 'physical_qualified', 'fips_validated',
               'deployment_permitted', 'file_contents_modified')


def same_directory(info):
    # chmod changes ctime and mode; no other identity/property may change.
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IFMT(info.st_mode), info.st_size, info.st_nlink, info.st_mtime_ns)


def checked_directory(info):
    u.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
              and stat.S_IMODE(info.st_mode) & ~0o022 == 0o755,
              'reviewed-root-owned-python-data-directory')


def harden_directory(path):
    u.hosted()  # Before external filesystem inspection or privileged commands.
    u.require(os.geteuid() != 0, 'unprivileged-python-data-setup-caller')
    u.require(path in DIRECTORIES, 'fixed-python-data-setup-directory')
    components.protected(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        checked_directory(before)
        u.require(same_directory(path.lstat()) == same_directory(before)
                  and components.identity(path.lstat()) == components.identity(before),
                  'stable-python-data-directory-before')
        changed = bool(before.st_mode & 0o022)
        if changed:
            # Fixed path/operation; remove only write bits, never recursively.
            status, _ = u.execute(['/usr/bin/sudo', '-n', '/usr/bin/chmod', 'go-w', '--', str(path)])
            u.require(status == 0, 'python-data-permission-removal-status')
        after = os.fstat(fd)
        named = path.lstat()
        u.require(same_directory(before) == same_directory(after) == same_directory(named)
                  and components.identity(after) == components.identity(named)
                  and stat.S_IMODE(after.st_mode) == 0o755,
                  'stable-protected-python-data-directory-after')
        components.protected(path)
        return dict(path=str(path), before_mode=format(stat.S_IMODE(before.st_mode), '04o'),
                    after_mode='0755', changed=changed, root_owned=True,
                    stable_directory_identity=True, protection_complete=True)
    finally:
        os.close(fd)


def data_files():
    components.protected(TARGET)
    fd = os.open(TARGET, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        u.require(stat.S_ISDIR(before.st_mode) and before.st_uid == 0,
                  'reviewed-python-locale-directory')
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                u.require(re.fullmatch(r'[A-Za-z0-9_]{1,64}[.]dat', entry.name)
                          and len(names) < LOCALE_FILE_COUNT, 'bounded-flat-python-locale-layout')
                names.append(entry.name)
        names.sort()
        encoded = json.dumps(names, separators=(',', ':')).encode()
        u.require(len(names) == LOCALE_FILE_COUNT and u.digest(encoded) == LOCALE_NAMES_SHA256,
                  'reviewed-python-locale-name-set')
        u.require(components.identity(before) == components.identity(os.fstat(fd))
                  == components.identity(TARGET.lstat()), 'stable-python-locale-directory')
        return [*FIXED_FILES, *(TARGET / name for name in names)]
    finally:
        os.close(fd)


def harden_file_batch(paths):
    u.hosted()
    u.require(os.geteuid() != 0, 'unprivileged-python-data-setup-caller')
    u.require(0 < len(paths) <= FILE_BATCH_SIZE and len(set(paths)) == len(paths)
              and all(path in FIXED_FILES or path.parent == TARGET
                      and re.fullmatch(r'[A-Za-z0-9_]{1,64}[.]dat', path.name) for path in paths),
              'bounded-python-data-file-batch')
    opened = []; changed = []; identities = {}
    with ExitStack() as stack:
        for path in paths:
            components.protected(path.parent)
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            stack.callback(os.close, fd)
            before = os.fstat(fd)
            u.require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                      and 0 <= before.st_size <= 64 * 1024 * 1024
                      and stat.S_IMODE(before.st_mode) & ~0o133 == 0o644,
                      'reviewed-root-owned-python-data-file')
            named = path.lstat()
            u.require(same_directory(named) == same_directory(before)
                      and components.identity(named) == components.identity(before),
                      'stable-python-data-file-before')
            opened.append((path, fd, before))
            if stat.S_IMODE(before.st_mode) != 0o644:
                changed.append(path)
        if changed:
            # Explicit flat names, at most 64 per command. Remove execute and
            # group/other-write bits from data files; never grant permissions.
            status, _ = u.execute(['/usr/bin/sudo', '-n', '/usr/bin/chmod', 'a-x,go-w', '--',
                                   *(str(path) for path in changed)])
            u.require(status == 0, 'python-data-file-permission-removal-status')
        for path, fd, before in opened:
            after = os.fstat(fd); named = path.lstat()
            u.require(same_directory(before) == same_directory(after) == same_directory(named)
                      and components.identity(after) == components.identity(named)
                      and stat.S_IMODE(after.st_mode) == 0o644,
                      'stable-protected-python-data-file-after')
            components.protected(path)
            identities[path] = (same_directory(after), components.identity(after))
    return Counter(format(stat.S_IMODE(before.st_mode), '04o') for _, _, before in opened), len(changed), identities


def harden_files():
    u.hosted()
    u.require(os.geteuid() != 0, 'unprivileged-python-data-setup-caller')
    paths = data_files(); modes = Counter(); changed = 0; identities = {}
    for start in range(0, len(paths), FILE_BATCH_SIZE):
        before, count, current = harden_file_batch(paths[start:start + FILE_BATCH_SIZE])
        modes.update(before); changed += count
        identities.update(current)
    u.require(paths == data_files(), 'unchanged-python-data-file-set')
    for path in paths:
        info = path.lstat()
        u.require((same_directory(info), components.identity(info)) == identities[path],
                  'unchanged-protected-python-data-files')
        components.protected(path)
    return dict(count=len(paths), locale_files=LOCALE_FILE_COUNT,
                locale_names_sha256=LOCALE_NAMES_SHA256, fixed_paths=[str(path) for path in FIXED_FILES],
                before_modes=dict(sorted(modes.items())), after_mode='0644', changed_files=changed,
                root_owned=True, single_link=True, stable_file_identities=True, protection_complete=True)


def harden():
    # Protect ancestors before any child. Unrelated trees remain untouched.
    directories = [harden_directory(path) for path in DIRECTORIES]
    return dict(directories=directories, files=harden_files())


def protection_diagnostic(error):
    if not isinstance(error, components.PathProtectionRefusal):
        return
    fixed = {*DIRECTORIES, *FIXED_FILES, *TARGET.parents}
    if error.requested not in fixed or error.ancestor not in fixed:
        return
    value = dict(schema=1, scope='fixed-python-data-protection-refusal', qualified=False,
                 requested_path=str(error.requested), failed_ancestor=str(error.ancestor),
                 symbolic_link=error.symbolic_link, root_owned=error.root_owned,
                 group_or_other_writable=error.group_or_other_writable)
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
    u.require(len(encoded.encode()) <= 1024, 'python-data-protection-diagnostic-budget')
    print('PYTHON_DATA_PROTECTION_PATH_DIAGNOSTICS ' + encoded, flush=True)


def source_inputs():
    return {name: u.file_digest(ROOT / name) for name in sorted(set(INPUTS))}


def observe_image_permissions(phase, report):
    u.hosted()
    u.require(os.geteuid() != 0, 'unprivileged-python-data-setup-caller')
    u.require(sys.flags.isolated == 1, 'isolated-python-data-setup')
    u.require(phase in OBSERVATION_PHASES, 'fixed-image-permission-phase')
    u.require(not os.path.lexists(report), 'new-image-permission-report')
    head = u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip()
    before = source_inputs()
    u.require(re.fullmatch('[0-9a-f]{40}', head)
              and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
              'clean-image-permission-checkout')
    rows = []
    for path in OBSERVATION_PATHS:
        try:
            info = path.lstat()
        except FileNotFoundError:
            rows.append(dict(path=str(path), kind='missing', mode=None, root_owned=None))
            continue
        kind = ('directory' if stat.S_ISDIR(info.st_mode) else
                'file' if stat.S_ISREG(info.st_mode) else
                'symlink' if stat.S_ISLNK(info.st_mode) else 'other')
        rows.append(dict(path=str(path), kind=kind,
                         mode=format(stat.S_IMODE(info.st_mode), '04o'),
                         root_owned=info.st_uid == 0))
    u.require(before == source_inputs()
              and u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == head
              and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
              'unchanged-image-permission-inputs')
    value = dict(schema=1, scope='fixed-hosted-image-permission-observation', phase=phase,
                 checkout=dict(clean=True, commit=head), inputs=before, paths=rows,
                 permissions_changed=False, advisory_findings_cleared=0,
                 **dict.fromkeys(FALSE_FLAGS, False))
    encoded = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    u.require(len(encoded) <= 16384, 'image-permission-report-budget')
    with os.fdopen(os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
        out.write(encoded)
    return value


def run(candidate_path, report):
    u.hosted()
    u.require(os.geteuid() != 0, 'unprivileged-python-data-setup-caller')
    u.require(sys.flags.isolated == 1, 'isolated-python-data-setup')
    u.require(not os.path.lexists(report), 'new-python-data-protection-report')
    raw, parent = candidate.read(candidate_path)
    head = u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip()
    before = source_inputs()
    u.require(parent['checkout'] == dict(clean=True, commit=head)
              and parent['inputs'] == candidate.source_inputs()
              and parent['advisory_status'] == 'complete'
              and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
              'current-python-data-setup-candidate')
    candidate.validate_advisory_result(parent, head)
    result = harden()
    u.require(before == source_inputs() and candidate.read(candidate_path)[0] == raw
              and parent['inputs'] == candidate.source_inputs()
              and u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == head
              and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
              'unchanged-python-data-setup-inputs')
    value = dict(schema=3, scope='hosted-disposable-python-data-permission-removal',
                 checkout=dict(clean=True, commit=head), inputs=before,
                 candidate_report_sha256=u.digest(raw), **result,
                 advisory_findings_cleared=0, checked_on=datetime.now(timezone.utc).isoformat(),
                 **dict.fromkeys(FALSE_FLAGS, False))
    encoded = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    u.require(len(encoded) <= 16384, 'python-data-setup-report-budget')
    with os.fdopen(os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
        out.write(encoded)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--candidate', type=Path)
    operation.add_argument('--observe-image-permissions', choices=OBSERVATION_PHASES)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.observe_image_permissions:
            observe_image_permissions(args.observe_image_permissions, args.report)
        else:
            run(args.candidate, args.report)
    except Exception as error:
        protection_diagnostic(error)
        category = str(error) if isinstance(error, u.Refusal) and re.fullmatch(r'[a-z0-9-]{1,100}', str(error)) else type(error).__name__
        parser.exit(1, 'Python data permission setup refused (' + category + ').\n')
    if args.observe_image_permissions:
        print('Fixed public image permission metadata observed; no permissions or contents changed.')
    else:
        print('Reviewed disposable Python data layout protected; contents and advisory gates unchanged.')


if __name__ == '__main__':
    main()
