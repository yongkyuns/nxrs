"""Run the patched native allocator algorithms with bounded host ABI stubs."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from test_esp_mbedtls_allocators import source_preimage


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/host/fixtures/heap-request-outcomes"
PATCH = ROOT / "upstream/nuttx/patches/0148-trace-heap-allocation-requests.patch"

STUBS = {
    "nuttx/config.h": r"""
#ifndef HOST_NUTTX_CONFIG_H
#define HOST_NUTTX_CONFIG_H
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#define FAR
#define CODE
#define inline_function inline
#define CONFIG_BUILD_FLAT 1
#define CONFIG_MM_BACKTRACE -1
#define CONFIG_MM_REGIONS 1
#define CONFIG_SMP_NCPUS 1
#define CONFIG_MM_NODE_GUARDSIZE 0
#ifndef CONFIG_MM_FREE_DELAYCOUNT_MAX
#define CONFIG_MM_FREE_DELAYCOUNT_MAX 0
#endif
#define MM_ALIGN 16
#define UNUSED(x) ((void)(x))
typedef unsigned int irqstate_t;
#endif
""",
    "nuttx/atomic.h": r"""
#ifndef HOST_ATOMIC_H
#define HOST_ATOMIC_H
#include <stdatomic.h>
#include <stdint.h>
typedef _Atomic int64_t atomic64_t;
#define atomic64_add_relaxed(p, n) atomic_fetch_add_explicit(p, n, memory_order_relaxed)
#define atomic64_read(p) atomic_load_explicit(p, memory_order_relaxed)
#endif
""",
    "nuttx/irq.h": r"""
#include <nuttx/config.h>
irqstate_t enter_critical_section_notrace(void);
void leave_critical_section_notrace(irqstate_t);
irqstate_t up_irq_save(void);
void up_irq_restore(irqstate_t);
""",
    "nuttx/mutex.h": "typedef int mutex_t;\n",
    "nuttx/sched.h": "#define this_cpu() 0\n",
    "nuttx/fs/procfs.h": "",
    "nuttx/lib/math32.h": r"""
#define LOG2_CEIL(x) (8 * sizeof(unsigned long) - __builtin_clzl((x) - 1))
#define flsl(x) (8 * sizeof(unsigned long) - __builtin_clzl(x))
""",
    "nuttx/arch.h": "#include <nuttx/irq.h>\n",
    "nuttx/debug.h": r"""
#include <assert.h>
#define DEBUGASSERT(x) assert(x)
#define DEBUGVERIFY(x) assert((x) >= 0)
#define DEBUGPANIC() ((void)test_panics++)
#define minfo(...) ((void)0)
#define mwarn(...) ((void)0)
""",
    "nuttx/mm/kasan.h": r"""
#ifndef HOST_KASAN_H
#define HOST_KASAN_H
#include <stddef.h>
static inline void kasan_poison(void *p, size_t n) { (void)p; (void)n; }
static inline void *kasan_unpoison(const void *p, size_t n) { (void)n; return (void *)p; }
#define kasan_clear_tag(p) (p)
#define kasan_get_tag(p) ((void)(p), 0)
#define kasan_set_tag(p, t) ((void)(t), (p))
#endif
""",
    "nuttx/mm/mempool.h": r"""
#ifndef HOST_MEMPOOL_H
#define HOST_MEMPOOL_H
#include <stddef.h>
struct mempool_multiple_s { int unused; };
struct mempool_s { int unused; };
struct mempoolinfo_s { unsigned long sizeblks, arena, aordblks, ordblks, iordblks, nwaiter; };
void *mempool_multiple_alloc(struct mempool_multiple_s *, size_t);
void *mempool_multiple_memalign(struct mempool_multiple_s *, size_t, size_t);
void *mempool_multiple_realloc(struct mempool_multiple_s *, void *, size_t);
int mempool_multiple_alloc_size(struct mempool_multiple_s *, void *);
int mempool_multiple_free(struct mempool_multiple_s *, void *);
void mempool_info(struct mempool_s *, struct mempoolinfo_s *);
#endif
""",
    "nuttx/mm/mm.h": r"""
#ifndef HOST_PUBLIC_MM_H
#define HOST_PUBLIC_MM_H
#include <nuttx/config.h>
struct mm_heap_s;
void *mm_malloc(struct mm_heap_s *, size_t);
void *mm_realloc(struct mm_heap_s *, void *, size_t);
void *mm_memalign(struct mm_heap_s *, size_t, size_t);
void mm_free(struct mm_heap_s *, void *);
bool mm_heapmember(struct mm_heap_s *, const void *);
size_t mm_malloc_size(struct mm_heap_s *, void *);
#include "request_operation.h"
#endif
""",
    "nuttx/sched_note.h": r"""
#ifndef HOST_SCHED_NOTE_H
#define HOST_SCHED_NOTE_H
#include <nuttx/config.h>
#include "outcome_notes.h"
#define NOTE_HEAP_ALLOC 20
#define NOTE_HEAP_FREE 21
void sched_note_heap(uint8_t, void *, void *, size_t, size_t);
#endif
""",
    "sys/param.h": "#define MIN(a, b) ((a) < (b) ? (a) : (b))\n",
}


def definition(source, marker, *, semicolon=False):
    """Retain the actual patched definition, including all nested branches."""
    start = source.index(marker)
    opening = source.index("{", start)
    level = 1
    index = opening + 1
    while level:
        if source[index] == "{":
            level += 1
        elif source[index] == "}":
            level -= 1
        index += 1
    if semicolon:
        assert source[index] == ";"
        index += 1
    return source[start:index] + "\n"


class SourceInventoryTests(unittest.TestCase):
    def test_allocator_sources_are_exact_pinned_upstream_inputs(self):
        manifest = json.loads((FIXTURE / "sources.json").read_text())
        release = json.loads((ROOT / "upstream/nuttx/release.json").read_text())
        self.assertEqual(manifest["revision"], release["revision"])
        self.assertEqual(len(manifest["files"]), 6)
        for item in manifest["files"]:
            with self.subTest(path=item["path"]):
                contents = (FIXTURE / "upstream" / item["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(contents).hexdigest(), item["sha256"])
                self.assertIn(b"SPDX-License-Identifier: Apache-2.0", contents)


@unittest.skipUnless(shutil.which("cc") and os.name != "nt",
                     "POSIX native C compiler and pthreads required")
class HeapRequestOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="heap-request-outcomes-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.tree = cls.root / "native"
        shutil.copytree(FIXTURE / "upstream", cls.tree)
        patch = PATCH.read_text()
        # Complete pinned allocator files exercise the actual branches. Other
        # files need only a preimage for extracting the new note bridge; the
        # independent QEMU gate compiles those complete native translation units.
        for path in re.findall(r"^\+\+\+ b/(.+)$", patch, re.M):
            target = cls.tree / path
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source_preimage(patch, path, expected_lines=None))
        subprocess.run(["git", "apply", str(PATCH)], cwd=cls.tree,
                       check=True, capture_output=True, text=True)
        include = cls.root / "include"
        for path, text in STUBS.items():
            target = include / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        public = (cls.tree / "include/nuttx/mm/mm.h").read_text()
        (include / "request_operation.h").write_text(
            "#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE\n" +
            definition(public, "enum mm_request_e", semicolon=True) +
            "#endif\n")
        header = (cls.tree / "include/nuttx/sched_note.h").read_text()
        notes = r"""
#ifndef HOST_OUTCOME_NOTES_H
#define HOST_OUTCOME_NOTES_H
#include <nuttx/config.h>
struct note_common_s { uint8_t nc_length, nc_type; int nc_pid; uint64_t nc_time; };
#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE
#define NOTE_HEAP_REQUEST 90
#define NOTE_HEAP_RESULT 91
"""
        for name in ("note_heap_request_s", "note_heap_result_s"):
            notes += definition(header, "struct " + name, semicolon=True)
        for name in ("sched_note_heap_request", "sched_note_heap_result"):
            notes += re.search(r"void " + name + r"\([^;]+;", header).group(0) + "\n"
        (include / "outcome_notes.h").write_text(notes + "#endif\n#endif\n")
        driver = (cls.tree / "drivers/note/note_driver.c").read_text()
        (include / "outcome_driver.c").write_text(
            "#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE\n" +
            "\n".join(definition(driver, "void " + name)
                      for name in ("sched_note_heap_request", "sched_note_heap_result")) +
            "#endif\n")
        cls.executables = {}
        for name, defines in {
                "enabled": ["CONFIG_MM_ALLOC_REQUEST_TRACE=1"],
                "disabled": [],
                "pools": ["CONFIG_MM_ALLOC_REQUEST_TRACE=1", "CONFIG_MM_HEAP_MEMPOOL=1"],
                "delayed": ["CONFIG_MM_ALLOC_REQUEST_TRACE=1", "CONFIG_MM_FREE_DELAYCOUNT_MAX=2"],
        }.items():
            executable = cls.root / name
            result = subprocess.run([
                "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                "-Wno-unused-function", "-Wno-unused-variable",
                "-Wno-unused-parameter", "-pthread",
                "-I", str(include), "-I", str(cls.tree / "mm"),
                *("-D" + value for value in defines),
                str(FIXTURE / "witness.c"), "-o", str(executable),
            ], capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(name + ": " + result.stderr)
            cls.executables[name] = executable

    def run_case(self, case, variant="enabled"):
        result = subprocess.run([str(self.executables[variant]), case],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native allocator outcome witness passed", result.stdout)

    def test_actual_allocator_success_failure_nested_and_zero_size_returns(self):
        self.run_case("ordinary")

    def test_runtime_filter_change_keeps_distinct_missing_endpoints(self):
        self.run_case("filtered")

    def test_heap_address_reuse_does_not_reuse_boot_request_ids(self):
        self.run_case("heap-reuse")

    def test_global_id_saturates_without_changing_allocator_or_heap_counts(self):
        self.run_case("saturation")

    def test_concurrent_callers_have_distinct_paired_ids(self):
        self.run_case("concurrent")

    def test_pool_fast_paths_report_their_actual_returns(self):
        self.run_case("pool", "pools")

    def test_delayed_reclamation_retry_preserves_nested_request_results(self):
        self.run_case("delayed", "delayed")

    def test_disabled_build_preserves_allocator_behaviour_and_has_no_trace_state(self):
        self.run_case("ordinary", "disabled")
        if shutil.which("nm"):
            symbols = subprocess.check_output(["nm", str(self.executables["disabled"])], text=True)
            # Mach-O nm prefixes C symbols with an underscore.
            self.assertNotRegex(symbols, r"\b_?(?:mm_request_begin|mm_request_end|g_mm_request_id|mm_alloc_requests)\b")


if __name__ == "__main__":
    unittest.main()
