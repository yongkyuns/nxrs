"""Check introductory project labeling and preserve existing reader links."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


class WordingTests(unittest.TestCase):
    def test_readme_labels_project_once_in_introduction(self):
        text = (ROOT / 'README.md').read_text()
        matches = list(re.finditer(r'\bexperimental\b', text, re.I))
        self.assertEqual(len(matches), 1)
        self.assertLess(matches[0].start(), text.index('## Why this project?'))

    def test_site_label_is_in_homepage_introduction_only(self):
        layout = (ROOT / 'website/layout.html').read_text()
        self.assertEqual(len(re.findall(r'\bexperimental\b', layout, re.I)), 1)
        lead = re.search(r'<p class="lead">(.*?)</p>', layout, re.S)
        self.assertIsNotNone(lead)
        self.assertRegex(lead.group(1), r'\bexperimental\b')
        for element in ('header', 'footer'):
            for content in re.findall(rf'<{element}\b[^>]*>(.*?)</{element}>', layout, re.S):
                self.assertNotRegex(content, r'(?i)\bexperiment(?:al)?\b')

    def test_existing_camera_fragment_remains_available(self):
        examples = (ROOT / 'website/guide/examples.md').read_text()
        self.assertIn('id="experimental-iot-camera"', examples)
        self.assertIn('## IoT camera', examples)


if __name__ == '__main__':
    unittest.main()
