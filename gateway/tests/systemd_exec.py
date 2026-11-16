"""Isolated executable placement for hosted systemd tests.

On some hosted Linux runners /run has noexec semantics. Keep private runtime
state there, but execute fixture binaries from the same /usr/local/libexec
filesystem used by the production units. Only random, owned test names.
"""
import os
from pathlib import Path
import re

LIBEXEC = Path('/usr/local/libexec')


def allocate(name):
    if not re.fullmatch(r'nxrs-(?:sd|ingest-test|full)-[0-9a-f]{8}', name):
        raise ValueError('unexpected hosted fixture name')
    # A minimal GitHub-hosted image need not pre-create this production path.
    # The parent is root-owned in the actual privileged fixture; do not use /run
    # for executable files because systemd may not execute them there.
    if os.path.lexists(LIBEXEC) and (LIBEXEC.is_symlink() or not LIBEXEC.is_dir()):
        raise AssertionError('hosted executable root is not a directory')
    LIBEXEC.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination = LIBEXEC/name
    if os.path.lexists(destination):
        raise AssertionError('hosted executable fixture already exists')
    destination.mkdir(mode=0o755)
    return destination
