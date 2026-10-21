"""Check allocator pairing at intr_alloc cleanup sites with a compact C witness."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_esp_mbedtls_allocators import source_preimage

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "upstream/esp-hal-3rdparty/patches/0002-intr-alloc-pair-capability-allocators.patch"
MBEDTLS_PATCH = ROOT / "upstream/esp-hal-3rdparty/patches/0001-mbedtls-pair-configured-allocators.patch"
APPLICATOR = ROOT / "tools/apply-nuttx-patches.py"
SOURCE = "components/esp_hw_support/intr_alloc.c"
MARKER_SOURCE = "components/mbedtls/port/esp_mem.c"


def patch_calls(patch_text, marker):
    """Return added/deleted deallocator statements from the target file hunks."""
    lines = patch_text.splitlines()
    index = lines.index(f"+++ b/{SOURCE}") + 1
    calls = []
    while index < len(lines) and not lines[index].startswith("diff --git "):
        line = lines[index]
        if line.startswith(marker) and re.search(r"\b(?:free|heap_caps_free)\s*\(", line):
            calls.append(line[1:].strip())
        index += 1
    return calls


@unittest.skipUnless(shutil.which("cc"), "native C compiler required")
class EspInterruptAllocatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="esp-intr-alloc-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "component"
        self.source = self.source_root / SOURCE
        self.source.parent.mkdir(parents=True)
        patch_text = PATCH.read_text()
        self.source.write_text(source_preimage(patch_text, SOURCE, expected_lines=None))
        marker = self.source_root / MARKER_SOURCE
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(source_preimage(MBEDTLS_PATCH.read_text(), MARKER_SOURCE))
        result = subprocess.run(
            [sys.executable, str(APPLICATOR), "--component", "esp-hal-3rdparty",
             "--source", str(self.source_root), "--revision", "fixture",
             "--record", str(self.root / "applied.json")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.patch_text = patch_text

    def compile_witness(self, name, calls):
        wrappers = []
        invocations = []
        for index, statement in enumerate(calls):
            variable = re.search(r"\((\w+)\)", statement).group(1)
            wrappers.append(
                f"static void release_{index}(void *ptr) "
                f"{{ void *{variable} = ptr; {statement} }}"
            )
            invocations.append(
                f"  alloc_domain = free_domain = 0;\n"
                f"  void *ptr_{index} = heap_caps_malloc(16, INTERNAL_8BIT);\n"
                f"  if (ptr_{index} != &allocation) return {index + 1};\n"
                f"  release_{index}(ptr_{index});\n"
                f"  if (alloc_domain != CAPS || free_domain != CAPS) return {index + 20};"
            )
        source = self.root / f"{name}.c"
        source.write_text(r"""
#include <stddef.h>
enum { INTERNAL_8BIT = 3, LIBC = 1, CAPS = 2 };
static int alloc_domain, free_domain;
static unsigned char allocation;
void mock_libc_free(void *ptr) { (void)ptr; free_domain = LIBC; }
#define free mock_libc_free
void *heap_caps_malloc(size_t size, int caps)
{ (void)size; alloc_domain = caps == INTERNAL_8BIT ? CAPS : 99; return &allocation; }
void heap_caps_free(void *ptr)
{ if (ptr != &allocation) __builtin_abort(); free_domain = CAPS; }
""" + "\n".join(wrappers) + "\nint main(void) {\n" +
                         "\n".join(invocations) + "\n  return 0;\n}\n")
        executable = self.root / name
        result = subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                 str(source), "-o", str(executable)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return subprocess.run([str(executable)], capture_output=True, text=True)

    def test_allocation_and_cleanup_sites_pair_with_capability_heap(self):
        patched = self.source.read_text()
        added = [line.strip() for line in patched.splitlines()
                 if re.search(r"\bheap_caps_free\s*\(", line)]
        expected = [
            ("no-interrupt error", "heap_caps_free(ret);"),
            ("no-vector error", "heap_caps_free(ret);"),
            ("shared-vector allocation error", "heap_caps_free(ret);"),
            ("nonshared trace-argument allocation error", "heap_caps_free(ret);"),
            ("success without returned handle", "heap_caps_free(ret);"),
            ("shared-vector teardown", "heap_caps_free(svd);"),
            ("nonshared trace-argument teardown", "heap_caps_free(isr_arg);"),
            ("normal handle teardown", "heap_caps_free(handle);"),
        ]
        self.assertEqual(added, [call for _, call in expected])
        self.assertEqual(len(re.findall(r"^\s*free\s*\(", patched, re.MULTILINE)), 0)
        self.assertIn('#include "esp_heap_caps.h"', patched)

        # This witness checks allocator pairing at the named cleanup sites;
        # it does not model or validate interrupt behavior.
        result = self.compile_witness("patched_pairing", added)
        self.assertEqual(result.returncode, 0, result.stderr)

        # The eight original libc-free statements must fail the same witness.
        removed = patch_calls(self.patch_text, "-")
        self.assertEqual(len(removed), 8)
        self.assertEqual(len([call for call in removed if call.startswith("free(")]), 8)
        old_result = self.compile_witness("old_mismatch", removed)
        self.assertNotEqual(old_result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
