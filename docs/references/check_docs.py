#!/usr/bin/env python3
"""Check section coverage and local links in the architecture references.

Uses only Python's standard library. External links are not HTTP-checked, and
structural consistency is not proof that an architectural explanation is correct.
"""
from __future__ import annotations
import html
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

TOPICS = [
    'Overview', 'Languages and runtime', 'APIs and abstraction boundaries',
    'Threads, scheduling and interrupts', 'Memory and data ownership',
    'Messages, data flow and wakeups', 'Drivers and sensor data',
    'Build, startup and shutdown', 'Debugging and performance',
    'What nxrs should borrow',
]


def unfenced(text: str) -> str:
    return re.sub(r'(?ms)^(```|~~~)[^\n]*\n.*?^\1[^\n]*$', '', text)


def anchors(text: str) -> set[str]:
    result = set(re.findall(r'<a\s+id="([^"]+)"', text))
    seen: dict[str, int] = {}
    for match in re.finditer(r'(?m)^#{1,6}\s+(.+)$', unfenced(text)):
        title = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', match[1])
        title = html.unescape(re.sub(r'<[^>]+>', '', title)).lower()
        key = re.sub(r'[^\w\- ]', '', title).replace(' ', '-')
        n = seen.get(key, 0)
        seen[key] = n + 1
        result.add(key if n == 0 else f'{key}-{n}')
    return result


def check(root: Path) -> dict:
    errors: list[str] = []
    counts: dict[str, int] = {}
    for system in ('openvela', 'zephyr', 'px4'):
        page = root / system / 'README.md'
        text = page.read_text()
        headings = list(re.finditer(r'(?m)^## (\d+)\. (.+)$', text))
        actual = [m[2] for m in headings]
        if actual != TOPICS:
            errors.append(f'{system}: unexpected section headings {actual}')
        if [int(m[1]) for m in headings] != list(range(1, 11)):
            errors.append(f'{system}: section numbers differ from 1..10')
        count = 0
        for i, match in enumerate(headings):
            end = headings[i+1].start() if i+1 < len(headings) else len(text)
            body = text[match.end():end]
            figures = re.findall(r'!\[[^\]]+\]\(diagrams/inline/[^)]+\.svg\)', body)
            if len(figures) != 1:
                errors.append(f'{system} section {match[1]}: {len(figures)} inline figures')
            count += len(figures)
        counts[system] = count
    files = sorted(root.rglob('*.md'))
    links = 0
    for page in files:
        text = unfenced(page.read_text())
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if (re.match(r'^\[[^\]]+\]:\s*\S', line) and i > 0
                    and lines[i-1].strip()
                    and not re.match(r'^\[[^\]]+\]:\s*\S', lines[i-1])):
                errors.append(f'{page.relative_to(root)}:{i+1}: reference definitions need a blank line before the group')
        defs = dict(re.findall(r'(?m)^\[([^\]]+)\]:\s*(\S+)', text))
        targets = re.findall(r'\]\(([^)\s]+)(?:\s+"[^"]*")?\)', text)
        for label in re.findall(r'\[[^\]\n]+\]\[([^\]\n]+)\]', text):
            if label not in defs:
                errors.append(f'{page.relative_to(root)}: undefined reference [{label}]')
            else:
                targets.append(defs[label])
        for raw in targets:
            url = urlsplit(html.unescape(raw.strip('<>')))
            if url.scheme or url.netloc:
                continue
            target = (page.parent / unquote(url.path)).resolve() if url.path else page
            links += 1
            if not target.exists():
                errors.append(f'{page.relative_to(root)}: missing {raw}')
                continue
            if url.fragment and target.suffix == '.md':
                fragment = unquote(url.fragment)
                if fragment not in anchors(target.read_text()):
                    errors.append(f'{page.relative_to(root)}: missing anchor {raw}')
    result = {'markdown_files': len(files), 'section_diagrams': counts,
              'local_links_and_anchors': links, 'errors': errors,
              'limits': 'Structural/local-link checks only; no external HTTP or semantic proof.'}
    return result


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    report = check(root)
    print(json.dumps(report, indent=2))
    (root/'reading-checks.json').write_text(json.dumps(report, indent=2)+'\n')
    raise SystemExit(bool(report['errors']))
