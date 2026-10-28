#!/usr/bin/env python3
"""Remove write permission from one reviewed disposable CI data directory.

The read-only inventory guard stays unchanged. This setup operation authenticates
no existing contents and grants no package, advisory or release qualification.
"""
import argparse
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


def harden():
    u.hosted()  # Before external filesystem inspection or privileged commands.
    u.require(os.geteuid() != 0, 'unprivileged-python-data-setup-caller')
    components.protected(TARGET.parent)
    fd = os.open(TARGET, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        checked_directory(before)
        u.require(same_directory(TARGET.lstat()) == same_directory(before)
                  and components.identity(TARGET.lstat()) == components.identity(before),
                  'stable-python-data-directory-before')
        changed = bool(before.st_mode & 0o022)
        if changed:
            # Fixed path/operation; remove only write bits, never recursively.
            status, _ = u.execute(['/usr/bin/sudo', '-n', '/usr/bin/chmod', 'go-w', '--', str(TARGET)])
            u.require(status == 0, 'python-data-permission-removal-status')
        after = os.fstat(fd)
        named = TARGET.lstat()
        u.require(same_directory(before) == same_directory(after) == same_directory(named)
                  and components.identity(after) == components.identity(named)
                  and stat.S_IMODE(after.st_mode) == 0o755,
                  'stable-protected-python-data-directory-after')
        components.protected(TARGET)
        return dict(path=str(TARGET), before_mode=format(stat.S_IMODE(before.st_mode), '04o'),
                    after_mode='0755', changed=changed, root_owned=True,
                    stable_directory_identity=True, protection_complete=True)
    finally:
        os.close(fd)


def source_inputs():
    return {name: u.file_digest(ROOT / name) for name in sorted(set(INPUTS))}


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
    value = dict(schema=1, scope='hosted-disposable-python-data-permission-removal',
                 checkout=dict(clean=True, commit=head), inputs=before,
                 candidate_report_sha256=u.digest(raw), directory=result,
                 advisory_findings_cleared=0, checked_on=datetime.now(timezone.utc).isoformat(),
                 **dict.fromkeys(FALSE_FLAGS, False))
    encoded = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    u.require(len(encoded) <= 16384, 'python-data-setup-report-budget')
    with os.fdopen(os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
        out.write(encoded)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.candidate, args.report)
    except Exception as error:
        category = str(error) if isinstance(error, u.Refusal) and re.fullmatch(r'[a-z0-9-]{1,100}', str(error)) else type(error).__name__
        parser.exit(1, 'Python data permission setup refused (' + category + ').\n')
    print('Reviewed disposable Python data directory protected; contents and advisory gates unchanged.')


if __name__ == '__main__':
    main()
