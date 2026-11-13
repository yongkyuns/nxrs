"""Keep navigation and source-backed documentation examples consistent."""
from collections import Counter
import json
from pathlib import Path
import re
import tomllib
import unittest
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]


def normalize(text):
    return '\n'.join(line.strip() for line in text.splitlines() if line.strip())


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'website/site.json').read_text())

    def test_sections_cover_each_guide_once(self):
        guides = [key for key, _, _ in self.config['guides']]
        grouped = [key for section in self.config['sections'] for key in section['pages']]
        self.assertEqual(len(guides), len(set(guides)))
        self.assertTrue(all(section['title'] and section['pages'] for section in self.config['sections']))
        self.assertEqual(Counter(grouped), Counter(guides))
        self.assertEqual(self.config['sections'][0]['pages'],
                         ['start-here', 'getting-started', 'first-application', 'firmware'])

    def test_all_guide_sources_have_navigation(self):
        sources = {path.stem for path in (ROOT / 'website/guide').glob('*.md')}
        self.assertEqual(sources, {key for key, _, _ in self.config['guides']})
        self.assertTrue({'motivation', 'start-here', 'status', 'examples', 'standard-library', 'io'} <= sources)

    def test_reading_path_links_exist(self):
        md = MarkdownIt('commonmark')
        sources = sorted((ROOT / 'website/guide').glob('*.md'))
        sources += [ROOT / 'README.md', ROOT / 'docs/README.md']
        for source in sources:
            for token in md.parse(source.read_text()):
                for child in token.children or []:
                    if child.type != 'link_open':
                        continue
                    href = child.attrGet('href')
                    parts = urlsplit(href)
                    if parts.scheme or parts.netloc or not parts.path:
                        continue
                    target = (source.parent / unquote(parts.path)).resolve()
                    self.assertTrue(target.is_relative_to(ROOT), (source, href))
                    self.assertTrue(target.exists(), (source, href))

    def test_application_excerpt_matches_implementation(self):
        guide = (ROOT / 'website/guide/first-application.md').read_text()
        source = (ROOT / 'website/examples/basic-firmware/src/main.rs').read_text()
        match = re.search(r'```rust\n(.*?)\n```', guide, re.S)
        self.assertIsNotNone(match)
        self.assertEqual(normalize(match.group(1)), normalize(source))
        manifest = re.search(r'```toml\n(.*?)\n```', guide, re.S)
        self.assertIsNotNone(manifest)
        actual = tomllib.loads((ROOT / 'website/examples/basic-firmware/Cargo.toml').read_text())
        self.assertEqual(tomllib.loads(manifest.group(1)), actual)

    def test_worker_excerpt_matches_implementation(self):
        guide = (ROOT / 'website/guide/services.md').read_text()
        source = (ROOT / 'website/examples/basic-firmware/examples/worker.rs').read_text()
        match = re.search(r'```rust\n(.*?)\n```', guide, re.S)
        self.assertIsNotNone(match)
        self.assertIn(normalize(match.group(1)), normalize(source))
        self.assertIn('sync_channel(4)', source)
        self.assertIn('.join()', source)
        self.assertIn('drop(sender);', source)

    def test_default_guide_is_domain_neutral(self):
        for name in ('start-here', 'getting-started', 'first-application', 'overview',
                     'standard-library', 'services', 'hardware', 'io', 'profiles', 'firmware'):
            text = (ROOT / 'website/guide' / f'{name}.md').read_text()
            self.assertNotRegex(text, r'(?i)\b(?:fusion|gnss|imu|robotics)\b', name)
            self.assertNotIn('nxrs-event-demo', text, name)
        for name in ('README.md', 'website/layout.html'):
            text = (ROOT / name).read_text()
            self.assertNotRegex(text, r'(?i)\b(?:fusion|gnss|imu|robotics)\b', name)
        defaults = {path for path, _ in self.config['reference']}
        self.assertNotIn('event-driven-demo.md', defaults)
        self.assertNotIn('camera-detection-delivery.md', defaults)

    def test_reference_groups_are_valid_and_unique(self):
        pages = [name for group in self.config['reference_groups'] for name in group['pages']]
        self.assertEqual(len(pages), len(set(pages)))
        for name in pages:
            self.assertTrue((ROOT / 'docs' / name).is_file(), name)

    def test_documented_std_cases_exist(self):
        source = (ROOT / 'app/std-demo/src/main.rs').read_text()
        cases = set(re.findall(r'\("([a-z]+)", (?:collections|concurrency)::', source))
        self.assertIn('channels', cases)
        command = r'(?:cargo [^\n`]* -p nxrs-std-demo|std_demo)\b[^\n`]* --case ([a-z]+)'
        for guide in (ROOT / 'website/guide').glob('*.md'):
            for case in re.findall(command, guide.read_text()):
                self.assertIn(case, cases | {'all'}, (guide.name, case))

    def test_site_has_no_embedded_application(self):
        layout = (ROOT / 'website/layout.html').read_text()
        script = (ROOT / 'website/static/site.js').read_text()
        self.assertIn("frame-src 'none'", layout)
        self.assertNotIn('<iframe', layout.lower())
        self.assertNotRegex(script, r'createElement\([\'\"]iframe[\'\"]\)')


if __name__ == '__main__':
    unittest.main()
