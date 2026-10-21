"""Exercise the registered ESP mbedTLS allocator patch with native stubs."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "upstream/esp-hal-3rdparty/patches/0001-mbedtls-pair-configured-allocators.patch"
APPLICATOR = ROOT / "tools/apply-nuttx-patches.py"
SOURCE = "components/mbedtls/port/esp_mem.c"
INTR_SOURCE = "components/esp_hw_support/intr_alloc.c"
INTR_PATCH = ROOT / "upstream/esp-hal-3rdparty/patches/0002-intr-alloc-pair-capability-allocators.patch"

STUBS = {
    "esp_attr.h": "",
    "sdkconfig.h": "",
    "esp_mem.h": "#include <stddef.h>\n",
    "platform/os.h": "",
    "esp_heap_caps.h": r"""
#include <stddef.h>
enum { MALLOC_CAP_INTERNAL = 1, MALLOC_CAP_8BIT = 2,
       MALLOC_CAP_SPIRAM = 4, MALLOC_CAP_IRAM_8BIT = 8 };
void *mock_libc_calloc(size_t, size_t);
void mock_libc_free(void *);
void *heap_caps_calloc(size_t, size_t, int);
void *heap_caps_calloc_prefer(size_t, size_t, int, int, int);
void heap_caps_free(void *);
""",
}

HARNESS = r"""
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "esp_heap_caps.h"

enum { LIBC = 1, CAPS = 2, CAPS_PREFER = 3 };
static unsigned char allocation[4096];
static int alloc_domain, free_domain, seen_caps, seen_caps2;
static int alloc_calls, free_calls, fail_next;

void *esp_mbedtls_mem_calloc(size_t, size_t);
void esp_mbedtls_mem_free(void *);

static void *record_alloc(int domain, int caps, int caps2)
{
  alloc_domain = domain;
  seen_caps = caps;
  seen_caps2 = caps2;
  alloc_calls++;
  if (fail_next) { fail_next = 0; return NULL; }
  memset(allocation, 0, sizeof(allocation));
  return allocation;
}

void *mock_libc_calloc(size_t n, size_t size)
{ (void)n; (void)size; return record_alloc(LIBC, 0, 0); }
void mock_libc_free(void *ptr)
{ if (ptr != allocation && ptr != NULL) __builtin_abort(); free_domain = LIBC; free_calls++; }
void *heap_caps_calloc(size_t n, size_t size, int caps)
{ (void)n; (void)size; return record_alloc(CAPS, caps, 0); }
void *heap_caps_calloc_prefer(size_t n, size_t size, int count, int caps, int caps2)
{ (void)n; (void)size; if (count != 2) __builtin_abort(); return record_alloc(CAPS_PREFER, caps, caps2); }
void heap_caps_free(void *ptr)
{ if (ptr != allocation && ptr != NULL) __builtin_abort(); free_domain = CAPS; free_calls++; }

static int check(int ok, const char *what)
{
  if (!ok) fprintf(stderr, "allocator assertion failed: %s\n", what);
  return ok ? 0 : 1;
}

int main(void)
{
#if defined(CONFIG_MBEDTLS_CUSTOM_MEM_ALLOC)
  return check(1, "custom allocator leaves port functions unused");
#else
  size_t size = TEST_SIZE;
  void *ptr = esp_mbedtls_mem_calloc(1, size);
  if (check(ptr == allocation, "allocation returned") ||
      check(alloc_calls == 1, "one allocation")) return 1;
#if defined(EXPECT_LIBC)
  if (check(alloc_domain == LIBC, "libc calloc") ||
      check(seen_caps == 0, "no capability flags")) return 1;
#elif defined(EXPECT_INTERNAL)
  if (check(alloc_domain == CAPS, "capability calloc") ||
      check(seen_caps == (MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT), "internal 8-bit caps")) return 1;
#elif defined(EXPECT_EXTERNAL)
  if (check(alloc_domain == CAPS, "capability calloc") ||
      check(seen_caps == (MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT), "SPIRAM 8-bit caps")) return 1;
#elif defined(EXPECT_IRAM_PREFER)
  if (check(alloc_domain == CAPS_PREFER, "preferred IRAM calloc") ||
      check(seen_caps == (MALLOC_CAP_INTERNAL|MALLOC_CAP_IRAM_8BIT), "IRAM preferred caps") ||
      check(seen_caps2 == (MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT), "fallback internal caps")) return 1;
#elif defined(EXPECT_IRAM_SMALL)
  if (check(alloc_domain == CAPS, "small IRAM allocation uses regular caps") ||
      check(seen_caps == (MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT), "small internal caps")) return 1;
#endif
  esp_mbedtls_mem_free(ptr);
#if defined(EXPECT_LIBC)
  if (check(free_domain == LIBC, "libc free matches libc calloc")) return 1;
#else
  if (check(free_domain == CAPS, "capability free matches capability calloc")) return 1;
#endif
  if (check(free_calls == 1, "one free")) return 1;

  fail_next = 1;
  ptr = esp_mbedtls_mem_calloc(1, size);
  if (check(ptr == NULL, "allocation failure returns NULL")) return 1;
  esp_mbedtls_mem_free(NULL);
  if (check(free_calls == 2, "NULL free is accepted") ||
#if defined(EXPECT_LIBC)
      check(free_domain == LIBC, "NULL uses libc free")
#else
      check(free_domain == CAPS, "NULL uses capability free")
#endif
     ) return 1;
  return 0;
#endif
}
"""


def source_preimage(patch_text, target=SOURCE, *, expected_lines=42):
    """Rebuild a complete source or sparse preimage from unified diff hunks."""
    lines = patch_text.splitlines()
    target_header = f"+++ b/{target}"
    try:
        index = lines.index(target_header) + 1
    except ValueError as error:
        raise AssertionError(f"patch does not modify {target}") from error
    while index < len(lines) and not lines[index].startswith("@@"):
        index += 1
    original = []
    while index < len(lines) and lines[index].startswith("@@"):
        match = re.match(r"@@ -(\d+)(?:,(\d+))? ", lines[index])
        if not match:
            raise AssertionError(f"invalid hunk header for {target}: {lines[index]}")
        old_line = int(match.group(1))
        index += 1
        while index < len(lines) and not lines[index].startswith(("@@", "--- ", "diff --git ")):
            line = lines[index]
            if line.startswith((" ", "-")):
                position = old_line - 1
                while len(original) <= position:
                    original.append("/* sparse preimage */")
                value = line[1:]
                if original[position] != "/* sparse preimage */" and original[position] != value:
                    raise AssertionError(f"overlapping hunks disagree for {target}:{old_line}")
                original[position] = value
                old_line += 1
            elif line.startswith("+") or line.startswith("\\"):
                pass
            else:
                break
            index += 1
    if expected_lines is not None and len(original) != expected_lines:
        raise AssertionError(f"expected {expected_lines} original source lines, reconstructed {len(original)}")
    return "\n".join(original) + "\n"


@unittest.skipUnless(shutil.which("cc") and shutil.which("nm"),
                     "native C compiler and symbol inspector required")
class EspMbedtlsAllocatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="esp-mbedtls-alloc-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "component"
        source = self.source_root / SOURCE
        intr_source = self.source_root / INTR_SOURCE
        source.parent.mkdir(parents=True)
        source.write_text(source_preimage(PATCH.read_text()))
        intr_source.parent.mkdir(parents=True)
        intr_source.write_text(source_preimage(INTR_PATCH.read_text(), INTR_SOURCE,
                                              expected_lines=None))
        for name, contents in STUBS.items():
            path = self.root / "include" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)

        result = subprocess.run(
            [sys.executable, str(APPLICATOR), "--component", "esp-hal-3rdparty",
             "--source", str(self.source_root), "--revision", "fixture",
             "--record", str(self.root / "applied.json")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.patched_source = source
        self.patched_intr_source = intr_source

    def compile_and_run(self, name, defines, *, size=1):
        source = self.root / f"{name}.c"
        source.write_text(HARNESS.replace("TEST_SIZE", str(size)))
        executable = self.root / name
        command = ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                   "-I", str(self.root / "include"), "-I", str(self.patched_source.parent),
                   "-Dcalloc=mock_libc_calloc", "-Dfree=mock_libc_free",
                   *[f"-D{define}" for define in defines],
                   str(source), str(self.patched_source), "-o", str(executable)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([str(executable)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        if "CONFIG_MBEDTLS_CUSTOM_MEM_ALLOC" in defines:
            symbols = subprocess.run([shutil.which("nm"), "-g", str(executable)],
                                     capture_output=True, text=True, check=True).stdout
            self.assertNotRegex(symbols, r"esp_mbedtls_mem_(?:calloc|free)")

    def test_default_libc_fallback_uses_matching_free(self):
        self.compile_and_run("default", ["EXPECT_LIBC"])
        self.compile_and_run("zero_options", ["EXPECT_LIBC", "CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC=0",
                                               "CONFIG_MBEDTLS_IRAM_8BIT_MEM_ALLOC=0"])

    def test_internal_allocator_uses_capability_heap(self):
        self.compile_and_run("internal", ["CONFIG_MBEDTLS_INTERNAL_MEM_ALLOC",
                                           "EXPECT_INTERNAL"])

    def test_external_allocator_uses_spiram_heap(self):
        self.compile_and_run("external", ["CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC",
                                           "EXPECT_EXTERNAL"])

    def test_iram_symmetric_content_threshold(self):
        base = ["CONFIG_MBEDTLS_IRAM_8BIT_MEM_ALLOC", "CONFIG_MBEDTLS_SSL_MAX_CONTENT_LEN=2048"]
        self.compile_and_run("symmetric_small", [*base, "EXPECT_IRAM_SMALL"], size=2047)
        self.compile_and_run("symmetric_boundary", [*base, "EXPECT_IRAM_PREFER"], size=2048)

    def test_iram_allocator_respects_small_and_asymmetric_thresholds(self):
        base = ["CONFIG_MBEDTLS_IRAM_8BIT_MEM_ALLOC",
                "CONFIG_MBEDTLS_ASYMMETRIC_CONTENT_LEN",
                "CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN=1024",
                "CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN=2048"]
        self.compile_and_run("iram_small", [*base, "EXPECT_IRAM_SMALL"], size=1023)
        self.compile_and_run("iram_in_boundary", [*base, "EXPECT_IRAM_PREFER"], size=1024)
        self.compile_and_run("iram_out_boundary", [*base, "EXPECT_IRAM_PREFER"], size=2048)
        base[2:4] = ["CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN=2048", "CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN=1024"]
        self.compile_and_run("iram_lower_out_boundary", [*base, "EXPECT_IRAM_PREFER"], size=1024)

    def test_git_copy_requires_explicit_opt_in_and_exact_clean_revision(self):
        self.patched_source.write_text(source_preimage(PATCH.read_text()))
        self.patched_intr_source.write_text(source_preimage(INTR_PATCH.read_text(), INTR_SOURCE,
                                                            expected_lines=None))
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.source_root), *args], text=True).strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "fixture")
        revision = git("rev-parse", "HEAD")
        command = [sys.executable, str(APPLICATOR), "--component", "esp-hal-3rdparty",
                   "--source", str(self.source_root), "--record", str(self.root / "git-applied.json")]
        before = {path: path.read_bytes() for path in
                  (self.patched_source, self.patched_intr_source)}
        for args in (["--revision", revision],
                     ["--revision", "wrong", "--build-checkout"]):
            result = subprocess.run([*command, *args], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.patched_source.write_text("/* unrecorded edit */\n" + before[self.patched_source].decode())
        result = subprocess.run([*command, "--revision", revision, "--build-checkout"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "git-applied.json").exists())
        for path, contents in before.items():
            path.write_bytes(contents)
        result = subprocess.run([*command, "--revision", revision, "--build-checkout"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.patched_source.read_bytes(), before[self.patched_source])
        self.assertNotEqual(self.patched_intr_source.read_bytes(), before[self.patched_intr_source])
        self.compile_and_run("git_default", ["EXPECT_LIBC"])

    def test_custom_allocator_configuration_compiles_out_port_functions(self):
        self.compile_and_run("custom", ["CONFIG_MBEDTLS_CUSTOM_MEM_ALLOC"])


if __name__ == "__main__":
    unittest.main()
