import importlib.util
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("comparison_charts", HERE / "render.py")
charts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(charts)


class ChartTests(unittest.TestCase):
    def test_full_capacity_not_traffic_peak(self):
        rows = charts.chart_data()["ram"]
        self.assertEqual([sum(parts) for _, parts in rows], [257184, 257576, 230936, 84864])
        self.assertEqual([parts[0] for _, parts in rows], [81920, 81920, 81920, 8192])
        self.assertTrue(all(parts[1] == 30196 for _, parts in rows))
        self.assertTrue(all(min(parts) > 0 for _, parts in rows))

    def test_final_images_and_matched_timer_cohorts(self):
        data = charts.chart_data()
        self.assertEqual(data["flash"][0][1], (176884, 214508))
        self.assertEqual(data["flash"][1][1], (177708, 214532))
        self.assertEqual(data["flash"][3][1], (69461, 182496))
        self.assertEqual([v for _, v in data["timer"]], [(21.9, 1.6905), (20.644, 2.238), (20.0845, 1.8015), (12.288, 1.507)])

    def test_accessible_deterministic_assets(self):
        for name, svg in charts.charts().items():
            with self.subTest(name=name):
                self.assertEqual((HERE / name).read_text(), svg)
                root = ET.fromstring(svg)
                self.assertEqual(root.attrib["role"], "img")
                self.assertIsNotNone(root.find("{http://www.w3.org/2000/svg}title"))
                self.assertIsNotNone(root.find("{http://www.w3.org/2000/svg}desc"))
                self.assertNotIn("/Users/", svg)

    def test_each_diagram_has_source_and_render(self):
        for source in HERE.glob("*.d2"):
            self.assertTrue(source.with_suffix(".svg").exists(), source.name)

    def test_analysis_local_links_and_anchors_resolve(self):
        document = HERE.parents[1] / "rtos-comparison.md"
        for link in re.findall(r'\[[^]]*\]\(([^)]+)\)', document.read_text()):
            if "://" in link:
                continue
            path, _, anchor = link.partition("#")
            target = (document.parent / path).resolve()
            with self.subTest(link=link):
                self.assertTrue(target.is_file())
                if anchor:
                    headings = re.findall(r'^#+ (.+)$', target.read_text(), re.M)
                    slugs = [re.sub(r'[^\w\- ]', '', h.lower()).replace(' ', '-') for h in headings]
                    self.assertIn(anchor, slugs)


if __name__ == "__main__":
    unittest.main()
