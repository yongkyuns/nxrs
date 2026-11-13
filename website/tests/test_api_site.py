import importlib.util
import json
from pathlib import Path
import shutil
import unittest

import test_api_artifact as api_fixtures
import test_build as site_fixtures


spec = importlib.util.spec_from_file_location('website_check', Path(__file__).parents[1] / 'check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
build = site_fixtures.build


class ApiSiteTests(unittest.TestCase):
    def setUp(self):
        site_fixtures.WebsiteBuildTests.setUp(self)
        self.root = self.root.resolve()
        self.out = self.out.resolve()
        self.artifact = Path(self.temp.name).resolve() / 'rust-api'
        api_fixtures.make_api_fixture(self.root, self.artifact)
        config_path = self.root / 'website/site.json'
        config = json.loads(config_path.read_text())
        config['guides'].append(['start-here', 'Guide index', 'Fixture'])
        config_path.write_text(json.dumps(config))
        (self.root / 'website/guide/start-here.md').write_text('# Guide index\n')
        (self.root / 'website/api.md').write_text('# Rust API reference\n\n<!-- api-catalog -->\n\n## Scope\n\nFixture.\n')
        for name in ['api-catalog.html', 'static/rust-api.css']:
            shutil.copyfile(Path(__file__).parents[1] / name, self.root / 'website' / name)

    def render(self, prefix='/nxrs/'):
        return build.Site(self.root, prefix, api_docs=self.artifact).build(self.out)

    def test_catalog_search_source_ranges_and_return_links_at_both_bases(self):
        for prefix in ['/nxrs/', '/']:
            with self.subTest(prefix=prefix):
                report = self.render(prefix)
                links = check.check(self.out, prefix)
                self.assertEqual(links['errors'], [])
                self.assertEqual(report['rust_api']['crates'], 2)
                self.assertEqual(report['rust_api']['html_pages'], 4)
                catalog = (self.out / 'api/index.html').read_text()
                self.assertIn(f'href="{prefix}api/demo/index.html"', catalog)
                self.assertIn('nxrs-demo', catalog)
                self.assertIn('A fixture capability.', catalog)
                for name, home, guide in [
                    ('demo/index.html', '../index.html', '../../guide/start-here/'),
                    ('src/demo/lib.rs.html', '../../index.html', '../../../guide/start-here/'),
                ]:
                    content = (self.out / 'api' / name).read_text()
                    self.assertIn(f'href="{home}">NXRS API reference</a>', content)
                    self.assertIn(f'href="{guide}">User guide</a>', content)
                search = json.loads((self.out / 'search.json').read_text())
                libraries = [item for item in search if item['section'] == 'Rust API']
                self.assertEqual(len(libraries), 2)
                self.assertEqual(libraries[0]['url'], prefix + 'api/demo/index.html')
                self.assertIn('api/demo/index.html', (self.out / 'sitemap.xml').read_text())

    def test_invalid_or_absent_artifact_preserves_existing_site(self):
        self.render()
        sentinel = self.out / 'keep.txt'
        sentinel.write_text('existing preview')
        with self.assertRaisesRegex(ValueError, '--api-docs'):
            build.Site(self.root).build(self.out)
        with self.assertRaisesRegex(ValueError, 'revision'):
            build.Site(self.root, revision='b' * 40, api_docs=self.artifact).build(self.out)
        (self.artifact / 'docs/demo/index.html').write_text('changed output')
        with self.assertRaisesRegex(ValueError, 'files'):
            self.render()
        self.assertEqual(sentinel.read_text(), 'existing preview')

    def test_site_cannot_replace_its_api_artifact(self):
        for output in [self.artifact, self.artifact / 'site', self.artifact.parent]:
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'overlap'):
                build.Site(self.root, api_docs=self.artifact).build(output)
        api_fixtures.api.validate_artifact(self.root, self.artifact)

    def test_literal_and_decoded_fragments_are_both_checked(self):
        self.render()
        path = self.out / 'api/helper/struct.Helper.html'
        path.write_text(path.read_text().replace('</main>',
            '<a id="impl-Borrow%3CT%3E-for-T" href="#impl-Borrow%3CT%3E-for-T">Borrow</a>'
            '<a id="impl-From&lt;T&gt;-for-T" href="#impl-From%3CT%3E-for-T">From</a></main>'))
        check.check(self.out, '/nxrs/')
        path.write_text(path.read_text().replace('href="#impl-Borrow%3CT%3E-for-T"',
                                                'href="#impl-Missing%3CT%3E-for-T"'))
        with self.assertRaisesRegex(ValueError, 'missing anchor'):
            check.check(self.out, '/nxrs/')

    def test_source_ranges_require_ordered_existing_endpoints(self):
        self.render()
        path = self.out / 'api/demo/index.html'
        original = path.read_text()
        for fragment in ['2-1', '1-3', '0-2']:
            with self.subTest(fragment=fragment):
                path.write_text(original.replace('#1-2', '#' + fragment))
                with self.assertRaisesRegex(ValueError, 'missing anchor'):
                    check.check(self.out, '/nxrs/')

    def test_source_structure_and_range_exceptions_stay_scoped(self):
        self.render()
        source = self.out / 'api/src/demo/lib.rs.html'
        original = source.read_text()
        source.write_text(original.replace('<main id="main-content">', '').replace('</main>', ''))
        with self.assertRaisesRegex(ValueError, 'expected one main'):
            check.check(self.out, '/nxrs/')
        source.write_text(original)
        ordinary = self.out / 'guide/start/index.html'
        ordinary.write_text('<main><a id="1">1</a><a id="2">2</a></main>')
        with self.assertRaisesRegex(ValueError, 'expected one main and at least one h1'):
            check.check(self.out, '/nxrs/')
        ordinary.write_text('<main><h1>Start</h1><a id="1">1</a><a id="2" href="#1-2">2</a></main>')
        with self.assertRaisesRegex(ValueError, 'missing anchor'):
            check.check(self.out, '/nxrs/')

    def test_rustdoc_redirects_require_a_real_destination(self):
        self.render()
        path = self.out / 'api/helper/old.html'
        path.write_text('<meta http-equiv="refresh" content="0;url=struct.Helper.html">')
        check.check(self.out, '/nxrs/')
        path.write_text('<meta http-equiv="refresh" content="0;url=struct.Missing.html">')
        with self.assertRaisesRegex(ValueError, 'missing local redirect target'):
            check.check(self.out, '/nxrs/')
        path.unlink()
        ordinary = self.out / 'guide/start/redirect.html'
        ordinary.write_text('<meta http-equiv="refresh" content="0;url=index.html">')
        with self.assertRaisesRegex(ValueError, 'expected one main'):
            check.check(self.out, '/nxrs/')

    def test_unknown_rustdoc_page_structure_is_rejected(self):
        path = self.artifact / 'docs/helper/struct.Helper.html'
        path.write_text('<!doctype html><html><head></head><body><h1>Helper</h1></body></html>')
        api_fixtures.write_receipt(self.root, self.artifact)
        with self.assertRaisesRegex(ValueError, 'unsupported rustdoc page structure'):
            self.render()

    def test_only_absent_async_rustdoc_implementation_hooks_are_omitted(self):
        html = ('<html><head><meta name="generator" content="rustdoc"></head><body>'
                '<script src="../trait.impl/demo/trait.Empty.js" async></script>'
                '<script src="../trait.impl/demo/trait.Present.js" async></script>'
                '<script src="../trait.impl/demo/trait.Unrelated.js" async></script>'
                '<script src="../trait.impl/demo/trait.Synchronous.js"></script>'
                '<script src="../static.files/required.js" async></script>'
                '<script src="https://example.test/trait.impl/demo/trait.Remote.js" async></script>'
                '</body></html>')
        files = {'trait.impl/demo/trait.Present.js': 'synthetic hash'}
        content, omitted = build.api_implementor_hooks(html, 'demo/trait.Empty.html', files)
        self.assertNotIn('src="../trait.impl/demo/trait.Empty.js"', content)
        self.assertIn('trait.Present.js', content)
        self.assertIn('trait.Unrelated.js', content)
        self.assertIn('trait.Synchronous.js', content)
        self.assertIn('static.files/required.js', content)
        self.assertIn('trait.Remote.js', content)
        self.assertEqual(omitted, [{'page': 'demo/trait.Empty.html',
                                   'script': 'trait.impl/demo/trait.Empty.js'}])
        self.assertEqual(build.api_implementor_hooks(html, 'demo/trait.Empty.html',
                         files | {'trait.impl/demo/trait.Empty.js': 'synthetic hash'}), (html, []))
        self.assertEqual(build.api_implementor_hooks(html, 'demo/index.html', files), (html, []))
        authored = html.replace('<meta name="generator" content="rustdoc">', '')
        self.assertEqual(build.api_implementor_hooks(authored, 'demo/trait.Empty.html', files), (authored, []))

    def test_missing_required_scripts_still_fail_the_site_check(self):
        self.render()
        path = self.out / 'api/helper/struct.Helper.html'
        path.write_text(path.read_text().replace('</head>', '<script src="missing.js"></script></head>'))
        with self.assertRaisesRegex(ValueError, 'missing local script target'):
            check.check(self.out, '/nxrs/')


if __name__ == '__main__':
    unittest.main()
