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
    if not re.fullmatch(r'nxrs-(?:sd|ingest-test)-[0-9a-f]{8}', name):
        raise ValueError('unexpected hosted fixture name')
    destination = LIBEXEC/name
    if os.path.lexists(destination):
        raise AssertionError('hosted executable fixture already exists')
    destination.mkdir(mode=0o755)
    return destination
