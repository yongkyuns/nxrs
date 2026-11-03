"""Execute the pinned scheduler decision, including the empty-ready regression."""
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "sched/sched/sched_setpriority.c"
PATCH = "0105-sched-migrate-affinity-with-empty-ready-queue.patch"

HARNESS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#define CONFIG_SMP 1
#define FAR
#define DEBUGASSERT assert
#define UNUSED(x) (void)(x)
#define SWITCH_EQUAL 0
struct tcb_s { uint8_t sched_priority; unsigned affinity; int cpu; };
static struct tcb_s task, idle, next, *current, *ready;
static int migrated, delivered;
static void *list_readytorun(void) { return ready; }
static void *dq_peek(void *p) { return p; }
static struct tcb_s *this_task(void) { return current; }
static int this_cpu(void) { return current->cpu; }
static bool nxsched_remove_readytorun(struct tcb_s *t) {
  assert(t == current); current = &idle; return true;
}
static bool nxsched_add_readytorun(struct tcb_s *t) {
  assert(!(t->affinity & (1u << t->cpu))); ++migrated; return false;
}
static bool nxsched_deliver_task(int a, int b, int policy) {
  (void)a; (void)b; (void)policy; ++delivered; return false;
}
static void up_switch_context(struct tcb_s *a, struct tcb_s *b) {
  assert(a == &idle && b == &task);
}
"""

CASES = r"""
int main(int argc, char **argv) {
  assert(argc == 2);
  int case_id = atoi(argv[1]);
  task = (struct tcb_s){100, case_id < 2 ? 1u : 2u, 1};
  next = (struct tcb_s){100, 3u, 0};
  current = &task; ready = case_id % 2 ? &next : NULL;
  nxsched_running_setpriority(&task, 90);
  return case_id < 2 ? migrated != 1 :
         (migrated != 0 || delivered != (case_id == 3) || task.sched_priority != 90);
}
"""


class SmpAffinityTests(unittest.TestCase):
    def test_actual_scheduler_migrates_without_another_ready_task(self):
        compiler = shutil.which("cc")
        if not compiler:
            self.skipTest("host C compiler required")
        series = runpy.run_path(str(ROOT / "tools/apply-nuttx-patches.py"))["SERIES"]["nuttx"]
        self.assertEqual(series.count(PATCH), 1)
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True).strip()
        original = subprocess.check_output(["git", "-C", str(ROOT / "external/nuttx"),
                                            "show", f"{revision}:{SOURCE}"], text=True)
        with tempfile.TemporaryDirectory(prefix="nxrs-smp-affinity-") as directory:
            work = Path(directory)
            source = work / SOURCE
            source.parent.mkdir(parents=True)
            source.write_text(original)
            subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream/nuttx/patches" / PATCH)],
                           cwd=work, check=True, capture_output=True)
            for label, text in (("original", original), ("patched", source.read_text())):
                start = text.index("static inline void nxsched_running_setpriority(")
                function = text[start:text.index("\n}\n", start) + 3]
                probe, binary = work / "probe.c", work / label
                probe.write_text(HARNESS + function + CASES)
                subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                                str(probe), "-o", str(binary)], check=True, capture_output=True)
                for case in range(4):
                    result = subprocess.run([str(binary), str(case)], timeout=5)
                    self.assertEqual(result.returncode, int(label == "original" and case == 0),
                                     f"{label}: affinity/priority case {case}")


if __name__ == "__main__":
    unittest.main()
