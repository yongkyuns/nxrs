import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('website_build', Path(__file__).parents[1] / 'build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class WebsiteBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        for directory in ['website/guide', 'website/static', 'docs/assets', 'app/demo']:
            (self.root / directory).mkdir(parents=True)
        (self.root / 'website/site.json').write_text(json.dumps({
            'name': 'NXRS', 'description': 'Fixture', 'url': 'https://example.test/nxrs/',
            'repository': 'https://github.com/yongkyuns/nxrs',
            'guides': [['start', 'Start', 'Fixture']], 'reference': [['a.md', 'Reference']]}))
        (self.root / 'website/layout.html').write_text('<main id="main"><h1>{{ title }}</h1>{{ content|safe }}</main>')
        (self.root / 'website/static/site.js').write_text('// fixture\n')
        (self.root / 'website/guide/start.md').write_text('# Start\n\n[Reference](../../docs/a.md#same)\n\n`std::thread` and shutdown.')
        (self.root / 'docs/a.md').write_text('# Reference\n\n## Same\n\nText.\n\n## Same\n\nMore text.\n\n|a|b|\n|-|-|\n|1|2|\n')
        (self.root / 'app/demo/main.rs').write_text('fn main() {}')
        for name in ['nxrs-logo.png', 'nxrs-architecture.svg', 'nxrs-architecture-dark.svg']:
            (self.root / 'docs/assets' / name).write_bytes(b'fixture media; not a rendered image')
        self.out = Path(self.temp.name) / 'output'

    def test_reference_sources_are_rendered_without_copying(self):
        report = build.Site(self.root).build(self.out)
        content = (self.out / 'guide/start/index.html').read_text()
        self.assertIn('/nxrs/reference/a/#same', content)
        self.assertEqual(report['guide_pages'], 1)
        self.assertEqual(report['reference_pages'], 1)
        self.assertIn('docs/a.md', report['source_sha256'])

    def test_duplicate_heading_ids_and_tables(self):
        build.Site(self.root).build(self.out)
        text = (self.out / 'reference/a/index.html').read_text()
        self.assertIn('id="same"', text)
        self.assertIn('id="same-1"', text)
        self.assertIn('<table>', text)

    def test_source_links_remain_source_links(self):
        site = build.Site(self.root, revision='abc123')
        self.assertEqual(site.resolve('../../app/demo/main.rs', 'website/guide/start.md'),
                         'https://github.com/yongkyuns/nxrs/blob/abc123/app/demo/main.rs')
        self.assertIn('/tree/abc123/app/demo', site.resolve('../../app/demo', 'website/guide/start.md'))

    def test_query_and_fragment_order(self):
        site = build.Site(self.root)
        self.assertTrue(site.resolve('../../app/demo/main.rs?plain=1#L1', 'website/guide/start.md').endswith('?plain=1#L1'))

    def test_external_links_unchanged(self):
        site = build.Site(self.root)
        for value in ['https://example.com/a?q=1#x', 'mailto:test@example.com', '#local']:
            self.assertEqual(site.resolve(value, 'docs/a.md'), value)

    def test_media_paths_keep_base_prefix(self):
        site = build.Site(self.root, '/preview/'); site.discover()
        self.assertEqual(site.resolve('assets/nxrs-logo.png', 'docs/a.md', True), '/preview/assets/docs/assets/nxrs-logo.png')

    def test_root_base_and_search_index(self):
        build.Site(self.root, '/').build(self.out)
        data = json.loads((self.out / 'search.json').read_text())
        self.assertTrue(all(x['url'].startswith(('/guide/', '/reference/')) for x in data))
        self.assertTrue(any('shutdown' in x['text'] for x in data))

    def test_safe_output_ownership(self):
        self.out.mkdir(); sentinel = self.out / 'important.txt'; sentinel.write_text('keep')
        with self.assertRaises(ValueError): build.Site(self.root).build(self.out)
        self.assertEqual(sentinel.read_text(), 'keep')
        for path in [self.root, self.root / 'docs', self.root / 'website', self.root / 'website/static/new']:
            with self.assertRaises(ValueError): build.Site(self.root).build(path)

    def test_symlink_output_refused(self):
        target = Path(self.temp.name) / 'target'; target.mkdir(); (target / '.nxrs-website').write_text('marker')
        self.out.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError): build.Site(self.root).build(self.out)
        self.assertTrue(target.is_dir())

    def test_deterministic_rebuild(self):
        build.Site(self.root).build(self.out)
        before = {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        build.Site(self.root).build(self.out)
        after = {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_invalid_base_paths(self):
        for value in ['nxrs', '//evil.test/', '/../../', '/x?bad=1', '/a b/']:
            with self.assertRaises(ValueError): build.base_path(value)

    def test_traversal_link_refused(self):
        with self.assertRaises(ValueError): build.Site(self.root).resolve('../../../secret', 'docs/a.md')

    def test_missing_approved_artwork_fails(self):
        (self.root / 'docs/assets/nxrs-logo.png').unlink()
        with self.assertRaises(ValueError): build.Site(self.root).build(self.out)

    def test_html_links_and_srcset(self):
        (self.root / 'docs/a.md').write_text('# A\n\n<picture><source srcset="assets/nxrs-logo.png"><img src="assets/nxrs-logo.png" alt="Logo"></picture>')
        build.Site(self.root).build(self.out)
        text = (self.out / 'reference/a/index.html').read_text()
        self.assertIn('srcset="/nxrs/assets/docs/assets/nxrs-logo.png"', text)
        self.assertIn('src="/nxrs/assets/docs/assets/nxrs-logo.png"', text)


if __name__ == '__main__': unittest.main()
