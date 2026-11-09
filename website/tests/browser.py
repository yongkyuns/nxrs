#!/usr/bin/env python3
"""Browser checks for a built site. Remote Noon is mocked unless explicitly opted in."""
from __future__ import annotations
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory, prefix, **kwargs):
        self.prefix = prefix
        super().__init__(*args, directory=directory, **kwargs)

    def translate_path(self, path):
        route = urlsplit(path).path
        if not route.startswith(self.prefix):
            return str(Path(self.directory) / '__outside_site__')
        return super().translate_path('/' + route[len(self.prefix):])

    def log_message(self, *_):
        pass


def run(site: Path, prefix: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    handler = partial(Handler, directory=str(site.resolve()), prefix=prefix)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    url = origin + prefix
    report = {'checks': [], 'live_noon': {'attempted': False, 'engine_qualified': False}}
    errors = []
    try:
        with sync_playwright() as p:
            launch = {'headless': True, 'args': ['--no-sandbox']}
            if os.environ.get('NXRS_CHROMIUM'):
                launch['executable_path'] = os.environ['NXRS_CHROMIUM']
            browser = p.chromium.launch(**launch)
            report['browser'] = browser.version
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, color_scheme='light')
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url, wait_until='networkidle')
            expect(page.locator('h1')).to_contain_text('Ordinary Rust')
            assert page.locator('iframe').count() == 0
            assert page.locator('.brand img').evaluate('(img) => img.complete && img.naturalWidth > 0')
            page.screenshot(path=str(output / 'home-light.png'), full_page=True)
            report['checks'].append('homepage and approved artwork')

            page.locator('#theme-toggle').click()
            expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
            page.reload(wait_until='networkidle')
            expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
            page.screenshot(path=str(output / 'home-dark.png'), full_page=True)
            page.locator('#theme-toggle').click()
            report['checks'].append('theme toggle and persisted preference')

            page.keyboard.press('/')
            expect(page.locator('#search-dialog')).to_be_visible()
            page.locator('#search-input').fill('profiles')
            expect(page.locator('#search-results a').first).to_be_visible()
            expect(page.locator('#search-results a').first).to_contain_text('Profiles')
            page.keyboard.press('Escape')
            expect(page.locator('#search-dialog')).not_to_be_visible()
            expect(page.locator('.search-open')).to_be_focused()
            report['checks'].append('keyboard search, ranked results and focus restoration')

            page.goto(url + 'guide/getting-started/', wait_until='networkidle')
            expect(page.locator('h1')).to_contain_text('Getting started')
            assert page.locator('.copy-code').count() > 0
            page.screenshot(path=str(output / 'guide-desktop.png'), full_page=True)
            page.locator('.page-navigation a').last.click()
            expect(page).to_have_url(url + 'guide/first-application/')
            report['checks'].append('deep links, code controls and guide pagination')

            requested = []
            def mock_noon(route):
                requested.append(route.request.url)
                route.fulfill(status=200, content_type='text/html', body='<html><body><h1>Local embed lifecycle fixture</h1></body></html>')
            context.route('https://yongkyuns.github.io/noon/**', mock_noon)
            page.goto(url + 'guide/noon/', wait_until='networkidle')
            assert requested == [] and page.locator('iframe').count() == 0
            expect(page.locator('#noon-direct')).to_have_attribute('href', 'https://yongkyuns.github.io/noon/?example=showcase-group-animation')
            page.locator('#load-noon').click()
            expect(page.locator('#noon-status')).to_contain_text('embedded page loaded')
            assert len(requested) == 1
            expect(page.locator('#noon-frame iframe')).to_have_attribute('title', 'Noon: Move the whole diagram — interactive animation playground')
            page.locator('#unload-noon').click()
            assert page.locator('iframe').count() == 0
            expect(page.locator('#load-noon')).to_be_focused()
            page.locator('#load-noon').click()
            expect(page.locator('#noon-status')).to_contain_text('embedded page loaded')
            assert len(requested) == 2
            page.locator('#unload-noon').click()
            report['checks'].append('Noon opt-in only, labeled iframe, load/unload/reload and direct fallback (mocked)')

            page.set_viewport_size({'width': 390, 'height': 844})
            routes = ['', 'reference/'] + [item['url'][len(prefix):] for item in json.loads((site / 'search.json').read_text()) if item['section'] == 'User guide']
            for route in routes:
                page.goto(url + route, wait_until='networkidle')
                width = page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
                assert width['document'] <= width['viewport'] + 1, (route, width)
            page.goto(url + 'guide/getting-started/', wait_until='networkidle')
            page.locator('#menu-toggle').click()
            expect(page.locator('#menu-toggle')).to_have_attribute('aria-expanded', 'true')
            expect(page.locator('#sidebar')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#menu-toggle')).to_have_attribute('aria-expanded', 'false')
            page.screenshot(path=str(output / 'guide-mobile.png'), full_page=True)
            page.goto(url, wait_until='networkidle')
            page.screenshot(path=str(output / 'home-mobile.png'), full_page=True)
            report['checks'].append(f'mobile layout without page overflow on {len(routes)} routes; menu and Escape')

            no_js = browser.new_context(java_script_enabled=False, viewport={'width': 1280, 'height': 900})
            plain = no_js.new_page()
            plain.goto(url + 'guide/noon/')
            expect(plain.locator('h1')).to_be_visible()
            expect(plain.locator('#noon-direct')).to_be_visible()
            assert plain.locator('iframe').count() == 0
            no_js.close()
            report['checks'].append('documentation and Noon direct link without JavaScript')

            if os.environ.get('NXRS_TEST_LIVE_NOON') == '1':
                report['live_noon']['attempted'] = True
                live = browser.new_page()
                try:
                    response = live.goto('https://yongkyuns.github.io/noon/?example=showcase-group-animation', timeout=45000, wait_until='domcontentloaded')
                    report['live_noon'].update(page_status=response.status if response else None, page_title=live.title(), note='Page response only; engine playback is not qualified by this test.')
                except Exception as error:
                    report['live_noon']['error'] = str(error)
                live.close()
            assert not errors, errors
            context.close()
            browser.close()
        report['result'] = 'passed'
    finally:
        server.shutdown()
        server.server_close()
        report['page_errors'] = errors
        (output / 'browser-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True, type=Path)
    parser.add_argument('--base-path', default='/nxrs/')
    parser.add_argument('--out', default=Path('target/website-browser'), type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.site, args.base_path, args.out), indent=2))
