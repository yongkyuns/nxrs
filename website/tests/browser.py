#!/usr/bin/env python3
"""Browser checks for the static NXRS site, without external applications."""
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
    report = {'checks': []}
    errors, external_requests = [], []
    guides = [item for item in json.loads((site / 'search.json').read_text()) if item['section'] == 'User guide']

    def observe_request(request):
        parsed = urlsplit(request.url)
        if parsed.scheme in ('http', 'https') and f'{parsed.scheme}://{parsed.netloc}' != origin:
            if request.resource_type in ('document', 'script', 'xhr', 'fetch'):
                external_requests.append(request.url)

    try:
        with sync_playwright() as p:
            launch = {'headless': True, 'args': ['--no-sandbox']}
            if os.environ.get('NXRS_CHROMIUM'):
                launch['executable_path'] = os.environ['NXRS_CHROMIUM']
            browser = p.chromium.launch(**launch)
            report['browser'] = browser.version
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, color_scheme='light')
            context.on('request', observe_request)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url, wait_until='networkidle')
            expect(page.locator('h1')).to_have_text('NXRS')
            expect(page.locator('.lead')).to_contain_text('experimental')
            assert page.locator('body').inner_text().lower().count('experimental') == 1
            expect(page.locator('#validation-title')).to_have_text('Implementation and validation')
            assert page.locator('iframe').count() == 0
            assert page.locator('.brand img').evaluate('(img) => img.complete && img.naturalWidth > 0')
            page.screenshot(path=str(output / 'home-light.png'), full_page=True)
            report['checks'].append('project homepage, introductory status and approved artwork')

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

            main_nav = page.get_by_role('navigation', name='Main navigation')
            main_nav.get_by_role('link', name='User guide', exact=True).click()
            expect(page).to_have_url(url + 'guide/start-here/')
            expect(page.locator('h1')).to_have_text('Guide index')
            expect(page.locator('#sidebar .nav-group')).to_have_count(5)
            for item in guides:
                expect(page.locator(f'#sidebar a[href="{item["url"]}"]')).to_have_count(1)
            expect(page.locator('#sidebar a[aria-current="page"]')).to_have_text('Guide index')
            page.screenshot(path=str(output / 'guide-index.png'), full_page=True)
            main_nav.get_by_role('link', name='Why NXRS?', exact=True).click()
            expect(page.locator('h1')).to_have_text('Why NXRS?')
            expect(page.locator('.breadcrumb')).to_contain_text('About the project')
            expect(page.locator('.page-navigation a[rel="next"]')).to_have_attribute('href', prefix + 'guide/overview/')
            page.screenshot(path=str(output / 'motivation-desktop.png'), full_page=True)
            page.locator('#sidebar').get_by_role('link', name='Current status', exact=True).click()
            expect(page.locator('h1')).to_have_text('Current status')
            expect(page.locator('#completed-validation')).to_be_visible()
            assert page.locator('.page-navigation a[rel="next"]').count() == 0
            main_nav.get_by_role('link', name='Examples', exact=True).click()
            expect(page.locator('h1')).to_have_text('NXRS examples')
            expect(page.locator('#iot-camera')).to_be_visible()
            report['checks'].append('primary navigation, complete grouped sidebar, breadcrumbs and within-section pagination')

            page.goto(url + 'guide/getting-started/', wait_until='networkidle')
            expect(page.locator('h1')).to_contain_text('Getting started')
            assert page.locator('.copy-code').count() > 0
            page.screenshot(path=str(output / 'guide-desktop.png'), full_page=True)
            page.locator('.page-navigation a[rel="next"]').click()
            expect(page).to_have_url(url + 'guide/first-application/')
            expect(page.locator('.prose pre').first).to_contain_text('imu.start_on(devices::primary_imu())?')
            report['checks'].append('deep links, code controls, source-backed example and guide pagination')

            page.set_viewport_size({'width': 390, 'height': 844})
            routes = ['', 'reference/'] + [item['url'][len(prefix):] for item in guides]
            for route in routes:
                page.goto(url + route, wait_until='networkidle')
                width = page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
                assert width['document'] <= width['viewport'] + 1, (route, width)
                assert page.locator('iframe').count() == 0
                for label in ('Why NXRS?', 'User guide', 'Examples', 'Reference', 'GitHub ↗'):
                    expect(main_nav.get_by_role('link', name=label, exact=True)).to_be_visible()
            page.goto(url + 'guide/getting-started/', wait_until='networkidle')
            page.locator('#menu-toggle').click()
            expect(page.locator('#menu-toggle')).to_have_attribute('aria-expanded', 'true')
            expect(page.locator('#sidebar')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#menu-toggle')).to_have_attribute('aria-expanded', 'false')
            expect(page.locator('#menu-toggle')).to_be_focused()
            page.screenshot(path=str(output / 'guide-mobile.png'), full_page=True)
            page.locator('#menu-toggle').click()
            page.locator('#sidebar').get_by_role('link', name='Why NXRS?', exact=True).click()
            expect(page).to_have_url(url + 'guide/motivation/')
            expect(page.locator('#menu-toggle')).to_have_attribute('aria-expanded', 'false')
            page.goto(url, wait_until='networkidle')
            page.screenshot(path=str(output / 'home-mobile.png'), full_page=True)
            report['checks'].append(f'mobile layout without page overflow on {len(routes)} routes; visible primary links, menu, Escape and navigation')

            no_js = browser.new_context(java_script_enabled=False, viewport={'width': 390, 'height': 844})
            no_js.on('request', observe_request)
            plain = no_js.new_page()
            plain.goto(url + 'guide/start-here/')
            expect(plain.locator('h1')).to_have_text('Guide index')
            expect(plain.locator('#sidebar')).to_be_visible()
            plain.locator('#sidebar').get_by_role('link', name='Why NXRS?', exact=True).click()
            expect(plain.locator('h1')).to_have_text('Why NXRS?')
            plain.locator('#sidebar').get_by_role('link', name='NXRS examples', exact=True).click()
            expect(plain.locator('h1')).to_have_text('NXRS examples')
            assert plain.locator('iframe').count() == 0
            no_js.close()
            report['checks'].append('mobile reading and grouped navigation without JavaScript')

            assert not external_requests, external_requests
            assert not errors, errors
            report['checks'].append('no external application, script or fetch requests')
            context.close()
            browser.close()
        report['result'] = 'passed'
    finally:
        server.shutdown()
        server.server_close()
        report['page_errors'] = errors
        report['external_requests'] = external_requests
        (output / 'browser-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True, type=Path)
    parser.add_argument('--base-path', default='/nxrs/')
    parser.add_argument('--out', default=Path('target/website-browser'), type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.site, args.base_path, args.out), indent=2))
