"""Execute the maintained SDK observers with controlled native counter values."""
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class ObserverTests(unittest.TestCase):
    def run_c(self, program):
        with tempfile.TemporaryDirectory(prefix='mpsc-observers-') as directory:
            root = Path(directory)
            (root / 'test.c').write_text(program)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Werror', str(root / 'test.c'), '-o', str(root / 'test')],
                           check=True, capture_output=True, timeout=30)
            subprocess.run([str(root / 'test')], check=True, timeout=5)

    def runtime(self, runtime):
        source = (HERE / f'sdk_{runtime}.c').read_text()
        return source[source.index('uint64_t mp_runtime('):source.index('int mp_stack(')]

    def test_nuttx_charged_time_retains_long_lifetimes_and_current_interval(self):
        header = '''#include <assert.h>
#include <stdint.h>
#include <time.h>
#define CONFIG_ARCH_CHIP_ESP32S3 1
#define CONFIG_ARCH_PERF_EVENTS 1
#define CONFIG_PERF_OVERFLOW_CORRECTION 1
#define CONFIG_SCHED_CRITMONITOR 1
#define CONFIG_SCHED_CRITMONITOR_MAXTIME_THREAD 0
#define CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ 240
typedef int irqstate_t;
struct tcb_s { clock_t run_time, run_start; } task;
static unsigned long hz=240000000;
static clock_t now;
static unsigned unlocked;
static irqstate_t enter_critical_section(void) { return 7; }
static void leave_critical_section(irqstate_t flags) { assert(flags==7); ++unlocked; }
static struct tcb_s *nxsched_get_tcb(uintptr_t tid) { return tid==1 || tid==2 ? &task : 0; }
static uintptr_t gettid(void) { return 1; }
static clock_t perf_gettime(void) { return now; }
static unsigned long perf_getfreq(void) { return hz; }
'''
        main = '''int main(void) {
task.run_time=24000000000LL; task.run_start=48000000000LL; now=48240000000LL;
assert(mp_runtime(1)==101000000000ULL);
assert(mp_runtime(2)==100000000000ULL);
assert(mp_runtime(9)==UINT64_MAX);
hz=160000000; assert(mp_runtime(1)==UINT64_MAX);
hz=240000000; task.run_time=-1; assert(mp_runtime(2)==UINT64_MAX);
assert(unlocked==5); return 0;
}'''
        self.run_c(header + self.runtime('nuttx') + main)

    def test_zephyr_uses_selected_counter_frequency_without_lifetime_overflow(self):
        header = '''#include <assert.h>
#include <stdint.h>
#define CONFIG_THREAD_RUNTIME_STATS 1
#define CONFIG_THREAD_RUNTIME_STATS_USE_TIMING_FUNCTIONS 1
#define CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC 240000000
typedef uintptr_t k_tid_t;
typedef struct { uint64_t execution_cycles; } k_thread_runtime_stats_t;
static uint64_t hz=240000000;
static int k_thread_runtime_stats_get(k_tid_t tid,k_thread_runtime_stats_t *s) {
 s->execution_cycles=24000000000ULL; return tid==1 ? 0 : -1;
}
static uint64_t timing_freq_get(void) { return hz; }
'''
        main = '''int main(void) {
assert(mp_runtime(1)==100000000000ULL);
assert(mp_runtime(9)==UINT64_MAX);
hz=160000000; assert(mp_runtime(1)==UINT64_MAX); return 0;
}'''
        self.run_c(header + self.runtime('zephyr') + main)

    def test_primary_sdk_runtime_is_unavailable(self):
        for runtime in ('nuttx', 'zephyr'):
            with self.subTest(runtime=runtime):
                self.run_c('#include <stdint.h>\n#include <assert.h>\n' + self.runtime(runtime)
                           + '\nint main(void) {assert(mp_runtime(1)==UINT64_MAX); return 0;}\n')


if __name__ == '__main__':
    unittest.main()
