#!/usr/bin/env python3
"""Check generated internal links, media, anchors and basic page structure offline."""
from __future__ import annotations
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.images = set(), [], []
        self.main_count = self.h1_count = 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        if tag == 'main': self.main_count += 1
        if tag == 'h1': self.h1_count += 1
        for key in ('href', 'src'):
            if attrs.get(key): self.links.append((tag, attrs[key]))
        if attrs.get('srcset'):
            self.links.extend((tag, part.strip().split()[0]) for part in attrs['srcset'].split(','))
        if tag == 'img': self.images.append(attrs)


def check(site: Path, prefix: str) -> dict:
    site = site.resolve()
    pages = {p.relative_to(site).as_posix(): Page(p.read_text(encoding='utf-8'))
             for p in sorted(site.rglob('*.html'))}
    errors, anchor_notes, checked = [], [], 0
    for name, page in pages.items():
        if page.main_count != 1 or not page.h1_count:
            errors.append(f'{name}: expected one main and at least one h1')
        source_url = 'https://nxrs.test' + prefix + name
        for tag, href in page.links:
            resolved = urlsplit(urljoin(source_url, href))
            if resolved.scheme != 'https' or resolved.netloc != 'nxrs.test': continue
            if not resolved.path.startswith(prefix):
                errors.append(f'{name}: URL escapes site base: {href}'); continue
            relative = unquote(resolved.path[len(prefix):])
            target = site / relative
            if resolved.path.endswith('/') or target.is_dir():
                target /= 'index.html'
            if not target.resolve().is_relative_to(site) or not target.is_file():
                errors.append(f'{name}: missing local {tag} target: {href}'); continue
            checked += 1
            target_name = target.relative_to(site).as_posix()
            fragment = unquote(resolved.fragment)
            if fragment and target_name in pages and fragment not in pages[target_name].ids:
                message = f'{name}: missing anchor: {href}'
                # Historical references retain their content. Report anchor drift,
                # but enforce anchors in the new guide and site-owned navigation.
                if name.startswith('reference/'):
                    anchor_notes.append(message)
                else:
                    errors.append(message)
        if name.startswith('guide/'):
            for image in page.images:
                if 'alt' not in image: errors.append(f'{name}: image needs alt text')
    index = json.loads((site / 'search.json').read_text())
    if not index or not all({'title', 'url', 'section', 'headings', 'text'} <= p.keys() for p in index):
        errors.append('missing or invalid search index')
    for item in index:
        if not item['url'].startswith(prefix): errors.append('search URL escapes site base')
    report = {'html_pages': len(pages), 'checked_local_links': checked, 'search_pages': len(index),
              'reference_anchor_notes': anchor_notes, 'errors': errors}
    if errors: raise ValueError(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('site', type=Path)
    parser.add_argument('--base-path', default='/nxrs/')
    args = parser.parse_args()
    report = check(args.site, args.base_path)
    (args.site / 'link-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
