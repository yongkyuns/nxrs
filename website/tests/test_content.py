"""Keep the reading paths and source-backed examples consistent with the site."""
from collections import Counter
import json
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'website/site.json').read_text())

    def test_sections_cover_each_guide_once(self):
        guides = [key for key, _, _ in self.config['guides']]
        grouped = [key for section in self.config['sections'] for key in section['pages']]
        self.assertEqual(len(guides), len(set(guides)))
        self.assertTrue(all(section['title'] and section['pages'] for section in self.config['sections']))
        self.assertEqual(Counter(grouped), Counter(guides))

    def test_all_guide_sources_have_navigation(self):
        sources = {path.stem for path in (ROOT / 'website/guide').glob('*.md')}
        self.assertEqual(sources, {key for key, _, _ in self.config['guides']})
        self.assertTrue({'motivation', 'start-here', 'status', 'examples'} <= sources)

    def test_reading_path_links_exist(self):
        md = MarkdownIt('commonmark')
        for name in ('motivation', 'start-here', 'status', 'examples', 'overview', 'first-application'):
            source = ROOT / 'website/guide' / f'{name}.md'
            for token in md.parse(source.read_text()):
                for child in token.children or []:
                    if child.type != 'link_open':
                        continue
                    href = child.attrGet('href')
                    parts = urlsplit(href)
                    if parts.scheme or parts.netloc or not parts.path:
                        continue
                    target = (source.parent / unquote(parts.path)).resolve()
                    self.assertTrue(target.is_relative_to(ROOT), (name, href))
                    self.assertTrue(target.exists(), (name, href))

    def test_application_excerpt_matches_implementation(self):
        guide = (ROOT / 'website/guide/first-application.md').read_text()
        source = (ROOT / 'app/event-demo/src/main.rs').read_text()
        match = re.search(r'```rust\n(.*?)\n```', guide, re.S)
        self.assertIsNotNone(match)
        normalize = lambda text: '\n'.join(line.strip() for line in text.splitlines() if line.strip())
        self.assertIn(normalize(match.group(1)), normalize(source))

    def test_site_has_no_embedded_application(self):
        layout = (ROOT / 'website/layout.html').read_text()
        script = (ROOT / 'website/static/site.js').read_text()
        self.assertIn("frame-src 'none'", layout)
        self.assertNotIn('<iframe', layout.lower())
        self.assertNotRegex(script, r'createElement\([\'\"]iframe[\'\"]\)')


if __name__ == '__main__':
    unittest.main()
