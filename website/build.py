#!/usr/bin/env python3
"""Build the NXRS static website from guides and the existing docs tree.

No network requests, Rust build, source mutation, or deployment happen here.
Every docs/*.md page is rendered from its original file. Repository-only links
remain GitHub links; the source documents are not copied into a second manual.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_api import load_catalog, validate_artifact

ROOT = Path(__file__).resolve().parents[1]
MEDIA = {'.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif'}


def base_path(value: str) -> str:
    if not re.fullmatch(r'/[A-Za-z0-9_/-]*', value) or '//' in value or '..' in value:
        raise ValueError('base path must be a simple absolute URL path')
    return value.rstrip('/') + '/'


def slug(text: str) -> str:
    text = re.sub(r'[^\w\s-]', '', text.lower(), flags=re.UNICODE)
    return re.sub(r'\s', '-', text)


def plain(token) -> str:
    return ''.join(child.content for child in (token.children or [])
                   if child.type in {'text', 'code_inline', 'image'})


def highlight_code(code: str, language: str, _attrs: str = '') -> str:
    try:
        lexer = get_lexer_by_name(language or 'text')
    except ClassNotFound:
        return ''  # Markdown-it safely escapes unsupported fenced languages.
    return highlight(code, lexer, HtmlFormatter(nowrap=True))


def api_navigation(content: str, relative: str) -> str:
    """Add return links without changing rustdoc's layout or search implementation."""
    depth = len(Path(relative).parts) - 1
    api_home = '../' * depth + 'index.html'
    site_home = '../' * (depth + 1)
    navigation = (f'<nav class="nxrs-api-nav" aria-label="NXRS documentation">'
                  f'<a href="{api_home}">NXRS API reference</a>'
                  f'<a href="{site_home}guide/start-here/">User guide</a></nav>')
    content, count = re.subn(r'(<(?:main|section)\b[^>]*\bid="main-content"[^>]*>)',
                             lambda match: match[0] + navigation, content, count=1)
    if not count:
        content, count = re.subn(r'(<main\b[^>]*>)', lambda match: match[0] + navigation,
                                 content, count=1)
    if not count:
        if re.search(r'<meta\b[^>]*http-equiv=["\']refresh["\']', content, re.I):
            return content  # Rustdoc redirects lead to a page with return navigation.
        raise ValueError(f'unsupported rustdoc page structure: {relative}')
    stylesheet = f'<link rel="stylesheet" href="{site_home}assets/rust-api.css">'
    if '</head>' not in content:
        raise ValueError(f'rustdoc page has no head: {relative}')
    return content.replace('</head>', stylesheet + '</head>', 1)


def api_implementor_hooks(content: str, relative: str, files: dict) -> tuple[str, list[dict]]:
    """Omit rustdoc's optional empty implementation requests, preserving real lists.

    Rustdoc 1.90 emits async hooks even when no documented crate supplies an
    implementation. Its runtime does not wait for these hooks. Check the complete
    artifact, after all crates have been documented, before removing a request.
    Every other script remains subject to the ordinary link/resource checks.
    """
    omitted = []
    if (not re.fullmatch(r'trait\.[A-Za-z_][A-Za-z0-9_]*\.html', Path(relative).name)
            or '<meta name="generator" content="rustdoc">' not in content):
        return content, omitted

    def optional_hook(match):
        href = urlsplit(html.unescape(match[1]))
        target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), unquote(href.path)))
        if (not (href.scheme or href.netloc or href.query or href.fragment)
                and target.startswith('trait.impl/')
                and posixpath.basename(target) == Path(relative).with_suffix('.js').name
                and target not in files):
            omitted.append(dict(page=relative, script=target))
            return ''
        return match[0]

    content = re.sub(r'<script\s+src="([^"]+)"\s+async(?:="")?\s*></script>',
                     optional_hook, content)
    return content, omitted


class Site:
    def __init__(self, root: Path, prefix: str = '/nxrs/', revision: str = 'main',
                 api_docs: Path | None = None):
        self.root = root.resolve()
        self.prefix = base_path(prefix)
        if not re.fullmatch(r'[A-Za-z0-9._/-]+', revision):
            raise ValueError('invalid source revision')
        self.revision = revision
        self.config = json.loads((self.root / 'website/site.json').read_text())
        self.api_docs = api_docs
        self.api_catalog = (load_catalog(self.root)
                            if (self.root / 'website/api.json').is_file() else None)
        self.api_manifest = None
        self.pages: dict[str, dict] = {}
        self.routes: dict[str, str] = {}
        self.media: dict[str, str] = {}
        self.search: list[dict] = []
        self.md = MarkdownIt('commonmark', {'html': True, 'highlight': highlight_code})
        self.md.enable(['table', 'strikethrough'])
        self.env = Environment(loader=FileSystemLoader(self.root / 'website'),
                               autoescape=select_autoescape(['html']),
                               undefined=StrictUndefined)

    def url(self, route: str = '') -> str:
        return self.prefix + quote(route.lstrip('/'), safe='/#-_.')

    def github(self, source: str, fragment: str = '') -> str:
        kind = 'tree' if (self.root / source).is_dir() else 'blob'
        return (f"{self.config['repository']}/{kind}/{quote(self.revision, safe='/')}/"
                + quote(source, safe='/') + (f'#{fragment}' if fragment else ''))

    def register(self, source: str, route: str, section: str, title: str = '') -> None:
        if route in self.routes.values():
            raise ValueError(f'duplicate route: {route}')
        file = self.root / source
        if file.is_symlink() or not file.is_file():
            raise ValueError(f'missing or linked content: {source}')
        content = file.read_text(encoding='utf-8')
        tokens = self.md.parse(content)
        first = next((tokens[i + 1] for i, t in enumerate(tokens[:-1])
                      if t.type == 'heading_open' and t.tag == 'h1'), None)
        self.routes[source] = route
        self.pages[source] = dict(source=source, route=route, section=section,
                                  title=title or (plain(first) if first else file.stem),
                                  tokens=tokens)

    def discover(self) -> None:
        for key, title, _ in self.config['guides']:
            self.register(f'website/guide/{key}.md', f'guide/{key}/', 'User guide', title)
        if self.api_catalog:
            self.register('website/api.md', 'api/', 'Rust API reference')
        for path in sorted((self.root / 'docs').rglob('*.md')):
            if path.is_symlink():
                raise ValueError(f'linked document: {path}')
            relative = path.relative_to(self.root / 'docs').as_posix()
            self.register('docs/' + relative, 'reference/' + relative[:-3] + '/', 'Reference')
        for path in sorted((self.root / 'docs').rglob('*')):
            if path.suffix.lower() in MEDIA and path.is_file():
                if path.is_symlink():
                    raise ValueError(f'linked media: {path}')
                source = path.relative_to(self.root).as_posix()
                self.media[source] = 'assets/' + source
        for name in ('nxrs-logo.png', 'nxrs-architecture.svg', 'nxrs-architecture-dark.svg'):
            if f'docs/assets/{name}' not in self.media:
                raise ValueError(f'missing approved artwork: {name}')

    def resolve(self, href: str, source: str, image: bool = False) -> str:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc or not parts.path:
            return href
        path = unquote(parts.path)
        if path.startswith('/'):
            return href  # Authored site URLs are validated in the generated-site check.
        target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        if target == '..' or target.startswith('../'):
            raise ValueError(f'link escapes repository in {source}: {href}')
        if target in self.routes:
            output = self.url(self.routes[target])
        elif target in self.media:
            output = self.url(self.media[target])
        elif image:
            # Unpublished source images remain explicitly repository-hosted assets.
            output = (f"https://raw.githubusercontent.com/yongkyuns/nxrs/{self.revision}/"
                      + quote(target, safe='/'))
        else:
            return self.github(target) + (f'?{parts.query}' if parts.query else '') + (f'#{parts.fragment}' if parts.fragment else '')
        return output + (f'?{parts.query}' if parts.query else '') + (f'#{parts.fragment}' if parts.fragment else '')

    def rewrite_html(self, value: str, source: str) -> str:
        def attribute(match):
            key, quote_char, value = match.groups()
            value = html.unescape(value)
            if key == 'srcset' and not value.startswith('data:'):
                candidates = []
                for candidate in value.split(','):
                    fields = candidate.strip().split(maxsplit=1)
                    if fields:
                        candidates.append(self.resolve(fields[0], source, True)
                                          + (' ' + fields[1] if len(fields) > 1 else ''))
                value = ', '.join(candidates)
            else:
                value = self.resolve(value, source, key != 'href')
            return f'{key}={quote_char}{html.escape(value, quote=True)}{quote_char}'
        return re.sub(r"\b(href|src|srcset)=([\"'])([^\"']+)\2", attribute, value)

    def render(self, page: dict) -> tuple[str, list[dict]]:
        tokens = page['tokens']
        headings, texts, used = [], [], {}
        for i, token in enumerate(tokens):
            if token.type == 'heading_open':
                title = plain(tokens[i + 1])
                identifier = slug(title)
                count = used.get(identifier, 0)
                used[identifier] = count + 1
                if count:
                    identifier += f'-{count}'
                token.attrSet('id', identifier)
                level = int(token.tag[1])
                if level in (2, 3):
                    headings.append(dict(id=identifier, title=title, level=level))
            if token.type in ('fence', 'code_block'):
                texts.append(token.content)
            if token.type == 'inline':
                texts.append(plain(token))
                for child in token.children or []:
                    if child.type == 'html_inline':
                        child.content = self.rewrite_html(child.content, page['source'])
                    key = 'href' if child.type == 'link_open' else 'src' if child.type == 'image' else None
                    if key and child.attrGet(key):
                        child.attrSet(key, self.resolve(child.attrGet(key), page['source'], key == 'src'))
            if token.type in ('html_block', 'html_inline'):
                token.content = self.rewrite_html(token.content, page['source'])
        self.search.append(dict(title=page['title'], url=self.url(page['route']),
                                section=page['section'], headings=' '.join(x['title'] for x in headings),
                                text=' '.join(texts)))
        content = self.md.renderer.render(tokens, self.md.options, {})
        if page['section'] == 'Rust API reference':
            marker = '<!-- api-catalog -->'
            if content.count(marker) != 1:
                raise ValueError('website/api.md must contain one api-catalog marker')
            catalog = self.env.get_template('api-catalog.html').render(
                groups=self.api_catalog['groups'], manifest=self.api_manifest,
                repository=self.config['repository'], url=self.url)
            content = content.replace(marker, catalog)
            headings += [dict(id='api-group-' + group['id'], title=group['title'], level=2)
                         for group in self.api_catalog['groups']]
            headings.sort(key=lambda item: content.index(f'id="{item["id"]}"'))
        return content, headings

    def build(self, output: Path) -> dict:
        if output.is_symlink():
            raise ValueError('output must not be a symlink')
        output = output.resolve()
        if output.is_relative_to(self.root) and not output.is_relative_to(self.root / 'target'):
            raise ValueError('in-repository builds must stay under target/')
        if self.api_catalog:
            if self.api_docs is None:
                raise ValueError('build the Rust API with website/build_api.py, then pass --api-docs target/rust-api')
            artifact = self.api_docs.resolve()
            if output.is_relative_to(artifact) or artifact.is_relative_to(output):
                raise ValueError('website output and Rust API artifact must not overlap')
            expected_revision = self.revision if re.fullmatch(r'[0-9a-f]{40}', self.revision) else None
            self.api_manifest = validate_artifact(self.root, self.api_docs,
                                                  expected_revision=expected_revision)
        # Only clean a previously marked website build, never a source tree.
        marker = output / '.nxrs-website'
        if output == self.root or output in self.root.parents or output == self.root / 'website' or output == self.root / 'docs':
            raise ValueError('refusing unsafe output directory')
        if output.exists() and any(output.iterdir()) and not marker.is_file():
            raise ValueError(f'refusing to clean unowned output directory: {output}')
        if output.is_symlink():
            raise ValueError('output must not be a symlink')
        if output.exists():
            shutil.rmtree(output)
        output.mkdir(parents=True)
        marker.write_text('NXRS website output\n')
        api_html_pages = 0
        omitted_hooks = []
        if self.api_manifest:
            for relative in self.api_manifest['files']:
                if relative == 'index.html':
                    continue  # The branded API catalog owns this route.
                source = self.api_docs / 'docs' / relative
                target = output / 'api' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.suffix == '.html':
                    content, omitted = api_implementor_hooks(source.read_text(encoding='utf-8'),
                                                             relative, self.api_manifest['files'])
                    omitted_hooks.extend(omitted)
                    target.write_text(api_navigation(content, relative), encoding='utf-8')
                    api_html_pages += 1
                else:
                    shutil.copyfile(source, target)
        self.discover()
        for source, dest in self.media.items():
            target = output / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / source, target)
        for path in (self.root / 'website/static').iterdir():
            if not path.is_file() or path.is_symlink():
                raise ValueError(f'unsupported static input: {path}')
            target = output / 'assets' / path.name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, target)
        (output / 'assets/highlight.css').write_text(HtmlFormatter(style='friendly').get_style_defs('.prose pre'))
        guides = [dict(title=title, route=f'guide/{key}/', note=note)
                  for key, title, note in self.config['guides']]
        reference = [dict(title=title, route=self.routes['docs/' + path])
                     for path, title in self.config['reference'] if 'docs/' + path in self.routes]
        shared = dict(config=self.config, url=self.url, guides=guides, references=reference,
                      revision=self.revision, logo=self.url(self.media['docs/assets/nxrs-logo.png']),
                      has_api=bool(self.api_manifest))
        template = self.env.get_template('layout.html')

        def emit(route: str, title: str, kind: str, content: str = '', toc=None, source=''):
            target = output / route / 'index.html'
            target.parent.mkdir(parents=True, exist_ok=True)
            related_key = self.config.get('reference_guides', {}).get(source.removeprefix('docs/'))
            related_guide = next((item for item in guides
                                  if item['route'] == f'guide/{related_key}/'), None)
            target.write_text(template.render(**shared, route=route, title=title, kind=kind,
                              content=content, toc=toc or [], source_url=self.github(source) if source else '',
                              related_guide=related_guide,
                              all_references=sorted((p for p in self.pages.values() if p['section'] == 'Reference'),
                                                    key=lambda p: p['title'].casefold())), encoding='utf-8')

        emit('', 'Rust firmware on NuttX', 'home')
        emit('reference/', 'Engineering reference', 'reference-index')
        for page in self.pages.values():
            content, toc = self.render(page)
            kind = ('guide' if page['section'] == 'User guide' else
                    'api-index' if page['section'] == 'Rust API reference' else 'reference')
            emit(page['route'], page['title'], kind,
                 content, toc, page['source'])
        if self.api_catalog:
            for package in self.api_catalog['packages']:
                self.search.append(dict(title=package['crate'] + ' · ' + package['package'],
                                        url=self.url('api/' + package['crate'] + '/index.html'),
                                        section='Rust API', headings=package['crate'],
                                        text=package['summary'] + ' ' + package['package']))
        emit('404/', 'Page not found', 'not-found')
        shutil.copyfile(output / '404/index.html', output / '404.html')
        (output / '.nojekyll').touch()
        (output / 'search.json').write_text(json.dumps(self.search, ensure_ascii=False), encoding='utf-8')
        api_routes = (['api/' + package['crate'] + '/index.html'
                       for package in self.api_catalog['packages']] if self.api_catalog else [])
        urls = [self.config['url'] + route for route in ['', 'reference/', *self.routes.values(), *api_routes]]
        (output / 'sitemap.xml').write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + ''.join(f'<url><loc>{html.escape(u)}</loc></url>' for u in urls) + '</urlset>\n')
        report = dict(revision=self.revision, guide_pages=len(guides),
                      reference_pages=sum(page['section'] == 'Reference' for page in self.pages.values()),
                      total_pages=len(self.pages)+3+api_html_pages, base_path=self.prefix,
                      source_sha256={source: hashlib.sha256((self.root/source).read_bytes()).hexdigest()
                                     for source in sorted(self.pages)})
        if self.api_manifest:
            report['rust_api'] = {key: self.api_manifest[key]
                                  for key in ('revision', 'dirty', 'toolchain', 'host', 'binding', 'features')}
            report['rust_api'].update(crates=len(self.api_catalog['packages']), html_pages=api_html_pages,
                omitted_implementor_hooks=omitted_hooks,
                artifact_sha256=hashlib.sha256((self.api_docs / 'manifest.json').read_bytes()).hexdigest())
        (output / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n')
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'target/website')
    parser.add_argument('--base-path', default='/nxrs/')
    parser.add_argument('--revision', default=os.environ.get('GITHUB_SHA', 'main'))
    parser.add_argument('--api-docs', type=Path, help='validated artifact from website/build_api.py')
    args = parser.parse_args()
    report = Site(ROOT, args.base_path, args.revision, args.api_docs).build(args.out)
    print(json.dumps(report | {'source_sha256': f"{len(report['source_sha256'])} Markdown sources"}, indent=2))


if __name__ == '__main__':
    main()
