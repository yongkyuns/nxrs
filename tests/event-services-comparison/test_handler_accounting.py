"""Execute the actual C hook logic with a deterministic host clock/OS seam."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class HandlerAccountingTests(unittest.TestCase):
    def test_switch_nested_irq_wrap_and_snapshot_accounting(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("host C compiler unavailable")
        source = (HERE / "handler-probe.c").read_text()
        clock = '  __asm__ volatile("rsr.ccount %0" : "=a"(value) : : "memory");'
        self.assertEqual(source.count(clock), 1)
        source = source.replace(clock, "  value = test_clock;")
        placement = '#define HP_IRAM __attribute__((section(".iram1")))'
        self.assertEqual(source.count(placement), 1)
        source = source.replace(placement, "#define HP_IRAM")
        stubs = {
            "nuttx/config.h": "#define CONFIG_SCHED_INSTRUMENTATION_SWITCH 1\n"
                              "#define CONFIG_SCHED_INSTRUMENTATION_IRQHANDLER 1\n",
            "nuttx/irq.h": "typedef unsigned irqstate_t;\n"
                           "static irqstate_t enter_critical_section(void) { return 0; }\n"
                           "static void leave_critical_section(irqstate_t f) { (void)f; }\n",
            "nuttx/sched.h": "#ifndef HP_TEST_SCHED\n#define HP_TEST_SCHED\n"
                             "#include <sys/types.h>\nstruct tcb_s { pid_t pid; };\n"
                             "static struct tcb_s test_task = {7};\n"
                             "#endif\n",
            "unistd.h": "#include <nuttx/sched.h>\n"
                        "static pid_t gettid(void) { return test_task.pid; }\n",
            "nuttx/note/note_driver.h": "#include <stdbool.h>\n#include <nuttx/sched.h>\n"
                "struct note_driver_s;\nstruct note_driver_ops_s {\n"
                "void (*suspend)(struct note_driver_s *,struct tcb_s *);\n"
                "void (*resume)(struct note_driver_s *,struct tcb_s *);\n"
                "void (*irqhandler)(struct note_driver_s *,int,void *,bool);\n};\n"
                "struct note_driver_s { const struct note_driver_ops_s *ops; };\n"
                "static int note_driver_register(struct note_driver_s *d) { (void)d; return 0; }\n",
        }
        main = r'''
uint32_t es_diag_original_now(void) { return test_clock; }
void es_diag_original_receive(unsigned id, const struct es_event *e, int result,
                              uint32_t start, uint32_t end) {
  (void)id; (void)e; (void)result; (void)start; (void)end;
}
void es_diag_original_resources(void) {}
int main(void) {
  test_clock = 40; es_probe_reset(); es_probe_register(0);
  test_clock = 100; hp_suspend(&hp_driver, &test_task);
  struct tcb_s other_task = {8};
  test_clock = 105; hp_resume(&hp_driver, &other_task);
  test_clock = 150; hp_irq(&hp_driver, 0, 0, true);
  test_clock = 160; hp_irq(&hp_driver, 0, 0, true);
  test_clock = 170; hp_irq(&hp_driver, 0, 0, false);
  test_clock = 180; hp_resume(&hp_driver, &test_task);
  test_clock = 190; hp_irq(&hp_driver, 0, 0, false);
  test_clock = 220; account(test_clock);
  assert(totals.running == 90 && totals.other == 50 && totals.irq == 40);
  assert(totals.switches == 1 && errors == 0 && irq_depth == 0);
  test_clock = 221; hp_irq(&hp_driver, 0, 0, false);
  assert(errors == 1); /* Unbalanced leave is detectable, never unsigned wrap. */
  test_clock = 0xfffffff0u; es_probe_reset(); es_probe_register(0);
  test_clock = 0x10; account(test_clock);
  assert(totals.running == 32 && totals.other == 0 && totals.irq == 0);
  test_clock = 1000; es_probe_reset(); es_probe_register(0);
  uint32_t started = es_now();
  test_clock = 1200; hp_irq(&hp_driver, 0, 0, true);
  test_clock = 1250; hp_irq(&hp_driver, 0, 0, false);
  test_clock = 1500; uint32_t finished = es_now();
  struct es_event event = {.kind=1, .peer=2, .sequence=3};
  es_record_receive(0,&event,0,started,finished);
  assert(row_count == 1 && errors == 0);
  assert(rows[0].observed == 500 && rows[0].running == 450 && rows[0].irq == 50);
  es_record_receive(0,&event,0,started+1,finished);
  assert(row_count == 1 && errors == 1); /* Mismatched timestamp pair refused. */
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, text in stubs.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            file = root / "accounting.c"
            file.write_text("#include <stdint.h>\n#include <assert.h>\n"
                            "static uint32_t test_clock;\n" + source + main)
            binary = root / "accounting"
            compiled = subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(root), "-I", str(HERE), str(file),
                            "-o", str(binary)], text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            subprocess.run([str(binary)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
