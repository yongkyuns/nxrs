#!/usr/bin/env python3
"""Test-only ExecStartPost probe. Emit modes/booleans, never credential contents."""
import json
import os
from pathlib import Path
import stat
import sys


def probe(state, outside, original):
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    checks = {'nonroot': os.geteuid() != 0,
              'no_capabilities': int(status['CapEff'].strip(), 16) == 0,
              'no_new_privileges': status['NoNewPrivs'].strip() == '1'}
    try:
        (outside/'forbidden').write_text('fixture-only attempted write outside permitted storage')
    except OSError:
        checks['outside_write_denied'] = True
    else:
        checks['outside_write_denied'] = False
    try:
        original.read_bytes()
    except OSError:
        checks['credential_source_denied'] = True
    else:
        checks['credential_source_denied'] = False
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
    checks['loaded_credentials_readable'] = bool((credentials/'config').read_bytes() and (credentials/'security').read_bytes())
    loaded = (credentials/'security').stat()
    staged = Path(os.environ['RUNTIME_DIRECTORY'])/'security.json'
    metadata = staged.stat()
    checks['staged_credential_private'] = (stat.S_ISREG(metadata.st_mode) and metadata.st_mode & 0o077 == 0
        and metadata.st_uid == os.geteuid() and metadata.st_nlink == 1 and staged.read_bytes() == (credentials/'security').read_bytes())
    result = {**checks, 'uid': os.geteuid(), 'loaded_mode': stat.S_IMODE(loaded.st_mode),
              'staged_mode': stat.S_IMODE(metadata.st_mode)}
    (state/'sandbox-proof.json').write_text(json.dumps(result))
    print('SANDBOX_PROBE ' + json.dumps(result, sort_keys=True), flush=True)
    if not all(checks.values()):
        raise AssertionError('sandbox contract failed')


if __name__ == '__main__':
    probe(*map(Path, sys.argv[1:]))
