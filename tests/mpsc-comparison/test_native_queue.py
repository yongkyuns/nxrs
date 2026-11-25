"""Run the actual C queue adapter with controlled public-API/TCB outcomes.

These host controls prove error translation, descriptor/name ownership and the
wait observer predicate. They do not emulate or measure NuttX queue behavior.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
HEADERS = {
    'nuttx/config.h': '''#pragma once
#ifndef CONFIG_MQ_MAXMSGSIZE
#define CONFIG_MQ_MAXMSGSIZE 64
#endif
''',
    'nuttx/fs/fs.h': '''#pragma once
struct inode { void *i_private; };
struct file { struct inode *f_inode; };
int file_get(int, struct file **);
int file_put(struct file *);
''',
    'nuttx/irq.h': '''#pragma once
typedef unsigned irqstate_t;
irqstate_t enter_critical_section(void);
void leave_critical_section(irqstate_t);
''',
    'nuttx/sched.h': '''#pragma once
enum {TSTATE_WAIT_SEM = 1, TSTATE_WAIT_MQNOTFULL, TSTATE_WAIT_MQNOTEMPTY};
struct tcb_s { int sched_priority, task_state; void *waitobj; };
struct tcb_s *nxsched_get_tcb(int);
''',
}


class NativeQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shutil.which('cc')
        if not cls.compiler:
            raise unittest.SkipTest('C compiler unavailable')
        cls.temporary = tempfile.TemporaryDirectory(prefix='mpsc-native-queue-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        for name, content in HEADERS.items():
            target = cls.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        cls.executable = cls.root / 'adapter'
        cls.command = [cls.compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                       '-U_FORTIFY_SOURCE', '-D_FORTIFY_SOURCE=3',
                       '-I', str(cls.root), '-I', str(HERE),
                       str(HERE / 'native_queue_host.c'), '-o', str(cls.executable)]
        result = subprocess.run(cls.command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)

    def run_case(self, name):
        result = subprocess.run([str(self.executable), name], check=True,
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), 'native queue adapter host control passed')

    def test_public_queue_arguments_and_both_capacity_sizes(self):
        for name in ('small', 'large'):
            with self.subTest(case=name):
                self.run_case(name)

    def test_partial_open_rolls_back_only_owned_resources(self):
        for name in ('first-open', 'second-open', 'third-open'):
            with self.subTest(case=name):
                self.run_case(name)

    def test_failed_rollback_does_not_forget_retained_descriptor(self):
        self.run_case('partial-cleanup')

    def test_destruction_attempts_all_resources_and_retains_failures(self):
        self.run_case('destruction')

    def test_full_interrupt_and_short_read_outcomes_are_distinct(self):
        self.run_case('errors')

    def test_waiting_requires_queue_identity_direction_and_priority(self):
        self.run_case('waiting')

    def test_invalid_sizes_never_open_resources(self):
        self.run_case('invalid')

    def test_target_configuration_must_admit_native_cases(self):
        for define in ('-DCONFIG_MQ_MAXMSGSIZE=32', '-DCONFIG_DISABLE_MQUEUE=1'):
            with self.subTest(define=define):
                result = subprocess.run([*self.command, define], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('must support 64-byte POSIX messages', result.stderr)


if __name__ == '__main__':
    unittest.main()
