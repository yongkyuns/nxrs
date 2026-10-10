import importlib.util
from pathlib import Path
import re
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


HERE = Path(__file__).resolve().parent
DIAGRAMS = ("service-loop", "execution-models", "latency-path")
MATERIAL_PAIRS = {
    "#E3F2FD": "#BBDEFB",  # Blue 50 / 100.
    "#E0F2F1": "#B2DFDB",  # Teal 50 / 100.
    "#ECEFF1": "#CFD8DC",  # Blue-grey 50 / 100.
    "#FAFAFA": "#E0E0E0",  # Grey 50 / 300.
}
spec = importlib.util.spec_from_file_location("comparison_charts", HERE / "render.py")
charts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(charts)


class ChartTests(unittest.TestCase):
    def test_full_capacity_not_traffic_peak(self):
        rows = charts.chart_data()["ram"]
        self.assertEqual([tuple(map(sum, pair)) for _, pair in rows],
                         [(247204, 221092), (247220, 221108),
                          (231492, 205380), (87284, 61172)])
        for index, (_, (full, lean)) in enumerate(rows):
            execution = 12512 if index == 3 else 81920
            self.assertEqual((full[0], lean[0]), (execution, execution))
            self.assertEqual((full[1], lean[1]), (480 * 64, 480 * 64))
            self.assertEqual(full[2], lean[2])
            self.assertEqual((full[3], lean[3]), (3040, 3040))
            self.assertEqual((full[4], lean[4]), ((30536, 4424), (30536, 4424),
                             (30544, 4432), (31171, 5059))[index])
            self.assertEqual(full[5:], lean[5:])
            self.assertEqual(sum(full) - sum(lean), 26112)
            self.assertGreater(min(lean), 0)

    def test_ram_chart_separates_queue_storage_without_dropping_a_segment(self):
        svg = charts.charts()["ram-capacity.svg"]
        root = ET.fromstring(svg)
        legends = [label.text for label in root.iter("{http://www.w3.org/2000/svg}text")
                   if label.attrib.get("y") in ("85", "103")]
        self.assertEqual(legends, ["Execution workspace", "Queue slots (chosen)",
                         "Queue/adapter controls", "Fixture app state", "Checker/coordinator",
                         "RAM code / vectors", "Runtime / not split"])
        segments = [rect for rect in root.iter("{http://www.w3.org/2000/svg}rect")
                    if rect.attrib.get("height") == "17" and rect.attrib.get("fill") in charts.RAM_COLORS]
        self.assertEqual(len(segments), 4 * 2 * 7)
        self.assertEqual(svg.count('rx="3"'), 8)
        self.assertNotIn("Everything else", svg)
        self.assertNotIn("Test fields", svg)
        self.assertIn("not a fixed or unavoidable minimum", svg)

    def test_ram_attribution_must_match_the_measured_elf_and_total(self):
        original = charts.json.loads
        for field in ("elf_sha256", "components"):
            def damaged(text):
                record = original(text)
                ledger = record["ram_attribution"]["cohorts"]["lean"]["nuttx-c-three"]
                if field == "elf_sha256":
                    ledger[field] = "0" * 64
                else:
                    ledger[field]["ram_code"] += 1
                return record
            with self.subTest(field=field), mock.patch.object(charts.json, "loads", damaged):
                with self.assertRaises(ValueError):
                    charts.ram_rows()

    def test_final_images_and_matched_timer_cohorts(self):
        data = charts.chart_data()
        self.assertEqual(data["flash"][0][1], (114667, 115835))
        self.assertEqual(data["flash"][1][1], (115483, 116651))
        self.assertEqual(data["flash"][2][1], (88167, 89319))
        self.assertEqual(data["flash"][3][1], (69697, 92362))
        self.assertEqual([v for _, v in data["timer"]], [(21.9, 1.6905), (20.644, 2.238), (20.0845, 1.8015), (12.288, 1.507)])
        image = ET.fromstring(charts.charts()["image-size.svg"])
        legends = [label.text for label in image.iter("{http://www.w3.org/2000/svg}text")
                   if label.attrib.get("y") == "85"]
        self.assertEqual(legends, ["Code + initialized data", "Gap-free package size"])

    def test_packages_must_match_the_measured_firmware(self):
        original = charts.cases
        for field in ("image_sha256", "elf_sha256"):
            def mismatched(filename):
                rows = original(filename)
                if filename == "esp32s3-image-packages-task-trim-2026-10-09.json":
                    rows["nuttx-c-three"][field] = "0" * 64
                return rows

            with self.subTest(field=field), mock.patch.object(charts, "cases", mismatched):
                with self.assertRaisesRegex(ValueError, "identities differ"):
                    charts.chart_data()

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
        for name in DIAGRAMS:
            self.assertTrue((HERE / f"{name}.d2").exists())
            self.assertTrue((HERE / f"{name}.svg").exists())

    def test_diagram_text_is_readable_at_document_width(self):
        # Increasing native font size alone can also increase the SVG width.
        # Check the effective size after fit-to-column scaling, not just pixels.
        for name in DIAGRAMS:
            with self.subTest(diagram=name):
                svg = (HERE / f"{name}.svg").read_text()
                root = ET.fromstring(svg)
                width = float(root.attrib["viewBox"].split()[2])
                sizes = [int(size) for size in re.findall(r'font-size:\s*(\d+)px', svg)]
                self.assertTrue(sizes, "no rendered text sizes found")
                scale = min(1, 720 / width)
                self.assertGreaterEqual(min(sizes) * scale, 16)
                self.assertLessEqual(max(sizes), 20)
                self.assertEqual(float(root.attrib["width"]), width)
                self.assertIn("height", root.attrib)

    def test_diagrams_have_compact_landscape_proportions(self):
        for name in DIAGRAMS:
            with self.subTest(diagram=name):
                root = ET.parse(HERE / f"{name}.svg").getroot()
                _, _, width, height = map(float, root.attrib["viewBox"].split())
                self.assertGreaterEqual(width / height, 1.25)
                self.assertLessEqual(width / height, 3)

    def test_nested_card_text_does_not_overpower_container_titles(self):
        for name in DIAGRAMS:
            root = ET.parse(HERE / f"{name}.svg").getroot()
            labels = list(root.iter("{http://www.w3.org/2000/svg}text"))
            with self.subTest(diagram=name):
                self.assertTrue(labels)
                self.assertTrue(all("text" in label.attrib.get("class", "").split()
                                    for label in labels))
                self.assertFalse(any("text-bold" in label.attrib.get("class", "").split()
                                     for label in labels))

    def test_material_palette_and_rounded_cards_are_rendered(self):
        for name in DIAGRAMS:
            with self.subTest(diagram=name):
                source = (HERE / f"{name}.d2").read_text()
                svg = (HERE / f"{name}.svg").read_text()
                self.assertRegex(source, r'(?m)^\.\.\.@material-theme(?:\.d2)?$')
                self.assertIn('fill="#E3F2FD"', svg)
                self.assertIn('fill="#E0F2F1"', svg)
                rectangles = ET.fromstring(svg).iter("{http://www.w3.org/2000/svg}rect")
                self.assertTrue(any(float(rect.attrib.get("rx", 0)) == 12 for rect in rectangles))

    def test_nested_containers_have_distinct_fills(self):
        root = ET.parse(HERE / "service-loop.svg").getroot()
        rectangles = [rect for rect in root.iter("{http://www.w3.org/2000/svg}rect")
                      if float(rect.attrib.get("rx", 0)) > 0]
        self.assertEqual({rect.attrib["fill"] for rect in rectangles},
                         set(MATERIAL_PAIRS))

    def test_card_outlines_lightly_accent_their_fill_hue(self):
        for name in DIAGRAMS:
            root = ET.parse(HERE / f"{name}.svg").getroot()
            for rect in root.iter("{http://www.w3.org/2000/svg}rect"):
                if not float(rect.attrib.get("rx", 0)):
                    continue
                with self.subTest(diagram=name, fill=rect.attrib["fill"]):
                    self.assertEqual(rect.attrib["stroke"], MATERIAL_PAIRS[rect.attrib["fill"]])
                    # D2 serializes stroke width as inline CSS; SVG's default is 1.
                    stroke = re.search(r'stroke-width:\s*(\d+)', rect.attrib.get("style", ""))
                    width = rect.attrib.get("stroke-width", stroke[1] if stroke else "1")
                    self.assertEqual(width, "1")

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

    def test_size_footnotes_resolve_in_each_comparison_document(self):
        repository = HERE.parents[2]
        documents = ("docs/rtos-comparison.md",
                     "docs/rust-std-footprint.md")
        for name in documents:
            content = (repository / name).read_text()
            references = set(re.findall(r'\[\^([\w-]+)\](?!:)', content))
            definitions = set(re.findall(r'^\[\^([\w-]+)\]:', content, re.M))
            with self.subTest(document=name):
                self.assertIn("flash-size", references)
                self.assertFalse(references - definitions)


if __name__ == "__main__":
    unittest.main()
