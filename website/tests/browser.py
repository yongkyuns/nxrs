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
from urllib.parse import urljoin, urlsplit
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
    errors, external_requests, failed_responses = [], [], []
    search = json.loads((site / 'search.json').read_text())
    guides = [item for item in search if item['section'] == 'User guide']
    libraries = [item for item in search if item['section'] == 'Rust API']

    def observe_request(request):
        parsed = urlsplit(request.url)
        if parsed.scheme in ('http', 'https') and f'{parsed.scheme}://{parsed.netloc}' != origin:
            if request.resource_type in ('document', 'script', 'xhr', 'fetch'):
                external_requests.append(request.url)

    def observe_response(response):
        if response.url.startswith(origin + '/') and response.status >= 400:
            failed_responses.append({'url': response.url, 'status': response.status})

    try:
        with sync_playwright() as p:
            launch = {'headless': True, 'args': ['--no-sandbox']}
            if os.environ.get('NXRS_CHROMIUM'):
                launch['executable_path'] = os.environ['NXRS_CHROMIUM']
            browser = p.chromium.launch(**launch)
            report['browser'] = browser.version
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, color_scheme='light')
            context.on('request', observe_request)
            context.on('response', observe_response)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url, wait_until='networkidle')
            expect(page.locator('h1')).to_have_text('NXRS')
            expect(page.locator('.lead')).to_contain_text('experimental')
            assert page.locator('body').inner_text().lower().count('experimental') == 1
            expect(page.locator('#validation-title')).to_have_text('Implementation and validation')
            assert 'fusion' not in page.locator('main').inner_text().lower()
            assert page.locator('iframe').count() == 0
            assert page.locator('.brand img').evaluate('(img) => img.complete && img.naturalWidth > 0')
            page.screenshot(path=str(output / 'home-light.png'), full_page=True)
            report['checks'].append('general-purpose homepage, introductory status and approved artwork')

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
            expect(page.locator('#sidebar .nav-group')).to_have_count(8)
            expect(page.locator('#sidebar .nav-heading').first).to_have_text('Getting started')
            for item in guides:
                expect(page.locator(f'#sidebar a[href="{item["url"]}"]')).to_have_count(1)
            expect(page.locator('#sidebar a[aria-current="page"]')).to_have_text('Guide index')
            assert page.locator('#sidebar a[href$="/reference/event-driven-demo/"]').count() == 0
            page.screenshot(path=str(output / 'guide-index.png'), full_page=True)
            page.locator('#sidebar').get_by_role('link', name='Why NXRS?', exact=True).click()
            expect(page.locator('h1')).to_have_text('Why NXRS?')
            expect(page.locator('.breadcrumb')).to_contain_text('Understand NXRS')
            expect(page.locator('.page-navigation a[rel="next"]')).to_have_attribute('href', prefix + 'guide/overview/')
            page.screenshot(path=str(output / 'motivation-desktop.png'), full_page=True)
            page.locator('#sidebar').get_by_role('link', name='Current status', exact=True).click()
            expect(page.locator('h1')).to_have_text('Current status')
            expect(page.locator('#completed-validation')).to_be_visible()
            assert page.locator('.page-navigation a[rel="next"]').count() == 0
            main_nav.get_by_role('link', name='Examples', exact=True).click()
            expect(page.locator('h1')).to_have_text('NXRS examples')
            expect(page.locator('#basic-firmware')).to_be_visible()
            expect(page.locator('#iot-camera')).to_be_visible()
            assert page.locator('#experimental-iot-camera').count() == 1
            assert page.locator('#navigation-and-service-composition').count() == 1
            report['checks'].append('primary navigation, grouped sidebar, breadcrumbs, pagination and existing example anchors')

            main_nav.get_by_role('link', name='Getting started', exact=True).click()
            expect(page).to_have_url(url + 'guide/getting-started/')
            expect(page.locator('h1')).to_contain_text('Getting started')
            expect(page.locator('.prose')).to_contain_text('nxrs-std-demo')
            assert page.locator('.copy-code').count() > 0
            page.screenshot(path=str(output / 'guide-desktop.png'), full_page=True)
            page.locator('.page-navigation a[rel="next"]').click()
            expect(page).to_have_url(url + 'guide/first-application/')
            expect(page.locator('.prose pre').first).to_contain_text('println!("Hello from NXRS")')
            expect(page.locator('.page-navigation a[rel="next"]')).to_have_attribute('href', prefix + 'guide/firmware/')
            page.screenshot(path=str(output / 'first-application.png'), full_page=True)
            page.locator('#sidebar').get_by_role('link', name='Standard-library APIs', exact=True).click()
            expect(page.locator('h1')).to_have_text('Standard-library APIs')
            page.locator('#sidebar').get_by_role('link', name='Networking and storage', exact=True).click()
            expect(page.locator('h1')).to_have_text('Networking and storage')
            page.locator('.page-navigation a[rel="next"]').click()
            expect(page).to_have_url(url + 'guide/build-system/')
            expect(page.locator('h1')).to_have_text('Build workflow')
            expect(page.locator('.breadcrumb')).to_contain_text('Configure and build')
            page.screenshot(path=str(output / 'build-workflow-desktop.png'), full_page=True)
            report['checks'].append('neutral onboarding, new subsystem guides, code controls and source-backed example')
            report['checks'].append('guide pagination crosses section boundaries')

            page.locator('#sidebar').get_by_role('link', name='Build a controller', exact=True).click()
            expect(page).to_have_url(url + 'guide/controller/')
            expect(page.locator('h1')).to_have_text('Build a controller')
            controller_figure = page.locator('.architecture-art').first
            controller_figure.scroll_into_view_if_needed()
            for theme in ('light', 'dark'):
                controller_image = controller_figure.locator(f'img.theme-{theme}')
                expect(controller_image).to_be_visible()
                expect(controller_image).to_have_js_property('complete', True)
                assert controller_image.evaluate('(img) => img.naturalWidth > 0')
                controller_figure.screenshot(path=str(output / f'controller-{theme}.png'))
                page.locator('#theme-toggle').click()
            report['checks'].append('controller guide navigation and loaded command-flow SVG in both themes')

            page.goto(url + 'reference/architecture/', wait_until='networkidle')
            expect(page.locator('.reference-note a')).to_have_text('Architecture overview')
            reference_picture = page.locator('.prose picture').first.locator('img')
            expect(reference_picture).to_be_visible()
            assert '-dark.svg' not in reference_picture.evaluate('(img) => img.currentSrc')
            page.locator('#theme-toggle').click()
            page.wait_for_function("() => document.querySelector('.prose picture img').currentSrc.endsWith('-dark.svg')")
            page.screenshot(path=str(output / 'reference-architecture-dark.png'), full_page=True)
            page.locator('#theme-toggle').click()
            report['checks'].append('engineering reference links back to guide; SVG picture follows explicit theme')

            main_nav.get_by_role('link', name='Reference', exact=True).click()
            expect(page.locator('.reference-category h2').first).to_have_text('Framework and build system')
            expected = sorted(item['url'] for item in search if item['section'] == 'Reference')
            actual = sorted(page.locator('.reference-list a').evaluate_all('(links) => links.map(a => a.getAttribute("href"))'))
            assert actual == expected, (actual, expected)
            report['checks'].append('categorized reference index retains every source exactly once')

            main_nav.get_by_role('link', name='Rust API', exact=True).click()
            expect(page.locator('h1')).to_have_text('Rust API reference')
            expect(page.locator('.api-catalog tbody tr')).to_have_count(len(libraries))
            expect(page.locator('#api-led')).to_contain_text('nxrs-led')
            page.screenshot(path=str(output / 'api-catalog-desktop.png'), full_page=True)
            page.keyboard.press('/')
            page.locator('#search-input').fill('nxrs-led')
            page.locator(f'#search-results a[href="{prefix}api/led/index.html"]').click()
            expect(page).to_have_url(url + 'api/led/index.html')
            expect(page.locator('h1')).to_contain_text('led')
            page.locator('main a.trait[href="trait.Led.html"]').click()
            expect(page.locator('[id="tymethod.set"]')).to_contain_text('fn set')
            api_nav = page.get_by_role('navigation', name='NXRS documentation')
            expect(api_nav.get_by_role('link', name='User guide', exact=True)).to_be_visible()
            for theme in ('light', 'dark'):
                page.emulate_media(color_scheme=theme)
                expect(page.locator('html')).to_have_attribute('data-theme', theme)
                page.screenshot(path=str(output / f'api-led-{theme}.png'), full_page=True)
            page.emulate_media(color_scheme='light')
            source_link = page.locator('[id="tymethod.set"] a.src')
            source_url = urljoin(page.url, source_link.get_attribute('href'))
            source_link.click()
            expect(page).to_have_url(source_url)
            expect(page.locator('pre.rust')).to_contain_text('fn set')
            api_nav.get_by_role('link', name='NXRS API reference', exact=True).click()
            expect(page).to_have_url(url + 'api/index.html')
            expect(page.locator('h1')).to_have_text('Rust API reference')
            report['checks'].append('API catalog, public trait and method, source link, return navigation and native themes')

            # These modules are explicitly included by the API build configuration.
            for route in ('camera/replay/', 'storage/recording/', 'transport/udp/'):
                page.goto(url + 'api/' + route + 'index.html', wait_until='networkidle')
                expect(page.locator('h1')).to_contain_text(route.rstrip('/').split('/')[-1])
                expect(api_nav.get_by_role('link', name='NXRS API reference', exact=True)).to_be_visible()
            report['checks'].append('documented replay, recording and UDP feature modules are readable')

            page.goto(url + 'api/peripheral/imu/trait.Imu.html', wait_until='networkidle')
            for crate in ('nxrs_bmi270', 'nxrs_lsm6dso'):
                expect(page.locator(f'#implementors-list a[href$="{crate}/struct.Device.html"]').first).to_be_visible()
            report['checks'].append('cross-crate chip implementations load into the peripheral contract')

            page.goto(url + 'api/imu/index.html', wait_until='networkidle')
            symbol_search = page.get_by_role('searchbox', name='Run search in the documentation')
            symbol_search.click()
            page.wait_for_function("() => typeof document.querySelector('.search-form')?.onsubmit === 'function'")
            symbol_search.fill('led::LedSet')
            symbol_search.press('Enter')
            symbol = page.locator('#search .search-results.active a[href$="led/struct.LedSet.html"]').first
            expect(symbol).to_be_visible()
            symbol.click()
            expect(page).to_have_url(url + 'api/led/struct.LedSet.html')
            expect(page.locator('#main-content h1')).to_contain_text('LedSet')
            api_nav.get_by_role('link', name='NXRS API reference', exact=True).click()
            expect(page.locator('h1')).to_have_text('Rust API reference')
            report['checks'].append('native rustdoc symbol search finds and opens an item in another crate')

            page.set_viewport_size({'width': 390, 'height': 844})
            routes = ['', 'reference/', 'api/'] + [item['url'][len(prefix):] for item in guides]
            for route in routes:
                page.goto(url + route, wait_until='networkidle')
                width = page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
                assert width['document'] <= width['viewport'] + 1, (route, width)
                assert page.locator('iframe').count() == 0
                for label in ('Getting started', 'User guide', 'Examples', 'Reference', 'Rust API', 'GitHub ↗'):
                    expect(main_nav.get_by_role('link', name=label, exact=True)).to_be_visible()
            page.goto(url + 'guide/getting-started/', wait_until='networkidle')
            expect(page.locator('.mobile-toc')).to_be_visible()
            page.locator('.mobile-toc summary').click()
            section_link = page.locator('.mobile-toc a').first
            section_target = section_link.get_attribute('href')
            section_link.click()
            expect(page).to_have_url(url + 'guide/getting-started/' + section_target)
            expect(page.locator(section_target)).to_be_visible()
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

            page.goto(url + 'api/led/trait.Led.html', wait_until='networkidle')
            width = page.evaluate('({viewport: innerWidth, document: document.documentElement.scrollWidth})')
            assert width['document'] <= width['viewport'] + 1, width
            expect(api_nav.get_by_role('link', name='NXRS API reference', exact=True)).to_be_visible()
            page.screenshot(path=str(output / 'api-led-mobile.png'), full_page=True)
            api_nav.get_by_role('link', name='User guide', exact=True).click()
            expect(page).to_have_url(url + 'guide/start-here/')
            report['checks'].append('generated API mobile layout and return to the guide')

            no_js = browser.new_context(java_script_enabled=False, viewport={'width': 390, 'height': 844})
            no_js.on('request', observe_request)
            no_js.on('response', observe_response)
            plain = no_js.new_page()
            plain.goto(url + 'guide/start-here/')
            expect(plain.locator('h1')).to_have_text('Guide index')
            expect(plain.locator('#sidebar')).to_be_visible()
            plain.locator('.mobile-toc summary').click()
            expect(plain.locator('.mobile-toc nav')).to_be_visible()
            plain.locator('#sidebar').get_by_role('link', name='Your first application', exact=True).click()
            expect(plain.locator('h1')).to_have_text('Your first application')
            plain.locator('#sidebar').get_by_role('link', name='NXRS examples', exact=True).click()
            expect(plain.locator('h1')).to_have_text('NXRS examples')
            assert plain.locator('iframe').count() == 0
            plain.locator('#sidebar').get_by_role('link', name='Rust API reference', exact=True).click()
            expect(plain.locator('h1')).to_have_text('Rust API reference')
            plain.locator('#api-led a').click()
            plain.locator('main a.trait[href="trait.Led.html"]').click()
            expect(plain.locator('[id="tymethod.set"]')).to_contain_text('fn set')
            plain.get_by_role('navigation', name='NXRS documentation').get_by_role('link', name='User guide', exact=True).click()
            expect(plain.locator('h1')).to_have_text('Guide index')
            no_js.close()
            report['checks'].append('mobile reading, grouped navigation and public API reading without JavaScript')

            assert not external_requests, external_requests
            assert not failed_responses, failed_responses
            assert not errors, errors
            report['checks'].append('no failed local resources or external application, script or fetch requests')
            context.close()
            browser.close()
        report['result'] = 'passed'
    finally:
        server.shutdown()
        server.server_close()
        report['page_errors'] = errors
        report['external_requests'] = external_requests
        report['failed_responses'] = failed_responses
        (output / 'browser-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True, type=Path)
    parser.add_argument('--base-path', default='/nxrs/')
    parser.add_argument('--out', default=Path('target/website-browser'), type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.site, args.base_path, args.out), indent=2))
