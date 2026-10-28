"""Project fixed launcher refusal roles/checks without exposing file names."""
import re

ROLES = frozenset(('selection', 'approval', 'binary', 'loader', 'libc', 'crypto', 'ssl'))
CHECKS = frozenset(('absolute-path', 'protected-parent', 'file-stat', 'file-type',
    'file-owner', 'file-writable', 'file-links', 'file-size', 'file-executable',
    'file-special-mode', 'file-open', 'file-descriptor-stat', 'file-replaced',
    'file-read', 'file-budget', 'file-descriptor-restat', 'file-restat', 'file-changed'))


def file_refusals(raw):
    result = set()
    for line in raw[:65536].decode('ascii', errors='replace').splitlines():
        match = re.fullmatch(r'nxrs-gateway-proxy: file-refusal \(([a-z-]+): ([a-z-]+)\)', line)
        if match and match[1] in ROLES and match[2] in CHECKS:
            result.add((match[1], match[2]))
    return [dict(role=role, check=check) for role, check in sorted(result)[:8]]
