"""Keep the evaluation reading path concise and its retained evidence reachable."""
from pathlib import Path
import json
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
ANALYSES = (
    "docs/rtos-comparison.md",
    "docs/rust-std-footprint.md",
    "tests/arithmetic-parity/RESULTS.md",
)
GUIDES = (
    "docs/std-channel-performance.md",
    "docs/nuttx-std.md",
    "docs/camera-pico-performance.md",
    "docs/camera-detector-alternatives.md",
    "docs/history/camera-pico-study.md",
    "docs/history/detector-screening-study.md",
    "docs/history/std-channel-campaign.md",
    "docs/history/native-rtos-study.md",
    "docs/history/rust-footprint-study.md",
    "docs/history/nuttx-std-integration.md",
    "tests/README.md",
    "tests/arithmetic-parity/README.md",
    "tests/embassy-comparison/README.md",
    "tests/event-services-comparison/README.md",
    "tests/event-services-comparison/RAM.md",
    "tests/rtos-characterization/WROOM-REVIEW.md",
    "tests/event-services-comparison/results/README.md",
    "tests/service-footprint/README.md",
    "tests/service-qualification/README.md",
    "tests/zephyr-comparison/README.md",
    "docs/assets/rtos-comparison/README.md",
    "docs/upstream-patchsets.md",
    "upstream/README.md",
    "upstream/rust-llvm/README.md",
    "upstream/rust-llvm/BUILDING.md",
    "upstream/rust-std/README.md",
)


class EvaluationDocumentationTests(unittest.TestCase):
    def test_fresh_wroom_memory_ledger_and_usable_stack_claim_reconcile(self):
        evidence = json.loads((ROOT / "tests/rtos-characterization/wroom-review-2026-11-17.json").read_text())
        matched = [run for run in evidence["ram"] if run["cohort"] == "minimum-usable-4k"]
        self.assertEqual(len(matched), 6)
        for run in matched:
            with self.subTest(build=run["build"], repetition=run["pass"]):
                self.assertEqual(sum(run["ledger"].values()),
                                 run["memory"]["whole_image_observed_peak_bytes"])
                for profile in run["profiles"]:
                    self.assertEqual(profile["errors"], 0)
                    stacks = profile["worker_stacks"]
                    if stacks["count"]:
                        self.assertEqual(stacks["count"], 20)
                        self.assertGreaterEqual(stacks["reserved_min"], 4096)

    def test_one_short_reading_path(self):
        # The reader-facing analyses are summaries, not an experiment diary.
        self.assertLessEqual(sum(len((ROOT / name).read_text().split())
                                 for name in ANALYSES), 4000)
        entry = (ROOT / "README.md").read_text()
        for name in ANALYSES[:2]:
            with self.subTest(document=name):
                self.assertIn(f"]({name})", entry)
        # Compiler investigations are supporting evidence, not a third front door.
        self.assertIn("arithmetic-parity/RESULTS.md", (ROOT / "docs/rtos-comparison.md").read_text())

    def test_local_links_and_markdown_anchors_resolve(self):
        for name in ANALYSES + GUIDES:
            document = ROOT / name
            for link in re.findall(r'\[[^]]*\]\(([^)]+)\)', document.read_text()):
                if re.match(r'^[a-z]+:', link):
                    continue
                path, _, anchor = link.partition("#")
                target = document if not path else document.parent / path
                with self.subTest(document=name, link=link):
                    self.assertTrue(target.is_file() or target.is_dir(), str(target))
                    if anchor and target.suffix == ".md":
                        headings = re.findall(r'^#+ (.+)$', target.read_text(), re.M)
                        slugs = [re.sub(r'[^\w\- ]', '', h.lower()).replace(' ', '-')
                                 for h in headings]
                        explicit_ids = re.findall(r'<a\s+id=["\']([^"\']+)["\']',
                                                  target.read_text())
                        self.assertIn(anchor, slugs + explicit_ids)

    def test_evidence_index_names_existing_records(self):
        index = ROOT / "tests/event-services-comparison/results/README.md"
        names = re.findall(r'^\| `([^`]+\.json)` \|', index.read_text(), re.M)
        self.assertTrue(names)
        for name in names:
            with self.subTest(record=name):
                self.assertTrue((index.parent / name).is_file())


if __name__ == "__main__":
    unittest.main()
