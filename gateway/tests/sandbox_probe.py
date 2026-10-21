#!/usr/bin/env python3
"""Test-only ExecStartPost probe. Record modes/booleans, never credential contents."""
import json
import os
from pathlib import Path
import stat
import sys


def probe(state, outside, original):
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    checks = {'nonroot': os.geteuid() != 0,
              'no_capabilities': int(status['CapEff'].strip(), 16) == 0,
              'no_new_privileges': status['NoNewPrivs'].strip() == '1',
              'outside_write_denied': False, 'credential_source_denied': False,
              'loaded_credentials_readable': False, 'staged_credential_private': False}
    modes = {}
    report = state/'sandbox-proof.json'
    def save():
        result = {**checks, **modes, 'uid': os.geteuid()}
        report.write_text(json.dumps(result))
    # Preserve earlier observations even if a later access check fails.
    save()
    try:
        (outside/'forbidden').write_text('fixture-only attempted write outside permitted storage')
    except OSError:
        checks['outside_write_denied'] = True
    try:
        original.read_bytes()
    except OSError:
        checks['credential_source_denied'] = True
    save()
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
    loaded_secret = None
    try:
        modes['loaded_mode'] = stat.S_IMODE((credentials/'security').stat().st_mode)
        loaded_secret = (credentials/'security').read_bytes()
        checks['loaded_credentials_readable'] = bool((credentials/'config').read_bytes() and loaded_secret)
    except OSError:
        pass  # Recorded as a failed required check, not silently accepted.
    save()
    staged = Path(os.environ['RUNTIME_DIRECTORY'])/'security.json'
    try:
        metadata = staged.stat()
        modes['staged_mode'] = stat.S_IMODE(metadata.st_mode)
        checks['staged_credential_private'] = (
            stat.S_ISREG(metadata.st_mode) and metadata.st_mode & 0o077 == 0
            and metadata.st_uid == os.geteuid() and metadata.st_nlink == 1
            and loaded_secret is not None and staged.read_bytes() == loaded_secret)
    except OSError:
        pass
    save()
    if not all(checks.values()):
        raise AssertionError('sandbox contract failed')


if __name__ == '__main__':
    probe(*map(Path, sys.argv[1:]))
