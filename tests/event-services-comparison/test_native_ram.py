"""Execute native allocation/lifecycle controls with host threads and mock queues.

Target TCB/coloration and allocator readings are deliberate mocks: the tests
check capture lifetime, attribution boundaries and cleanup, never target sizes.
"""
import os
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
CONTRACT_SPEC = importlib.util.spec_from_file_location("native_ram_contract", HERE / "ram_contract.py")
contract = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(contract)

HEADERS = {
    "nuttx/config.h": """#pragma once
#ifndef MOCK_NO_COLORATION
#define CONFIG_STACK_COLORATION 1
#endif
#define CONFIG_INIT_STACKSIZE 8192
#define CONFIG_IDLETHREAD_STACKSIZE 3072
#define CONFIG_ARCH_INTERRUPTSTACK 4096
""",
    "nuttx/sched.h": """#pragma once
#include <stddef.h>
struct tcb_s { size_t adj_stack_size; };
struct tcb_s *nxsched_self(void);
struct tcb_s *nxsched_get_tcb(int);
""",
    "nuttx/arch.h": """#pragma once
#include <stddef.h>
struct tcb_s;
size_t up_check_tcbstack(struct tcb_s *, size_t);
size_t up_check_intstack(int, size_t);
""",
    "nuttx/spinlock.h": """#pragma once
typedef unsigned irqstate_t;
static inline irqstate_t enter_critical_section(void) { return 0; }
static inline void leave_critical_section(irqstate_t flags) { (void)flags; }
""",
    "malloc.h": """#pragma once
struct mallinfo { int uordblks; };
struct mallinfo test_mallinfo(void);
#define mallinfo test_mallinfo
""",
}

# Parse the real declarations before redirecting calls. In particular, glibc's
# fortified mq_open/poll inline functions use assembler aliases that can bypass
# a command-line -D rename and accidentally reach the host kernel instead.
MOCK_REDIRECT = r"""
#define _POSIX_C_SOURCE 200809L
#include <mqueue.h>
#include <poll.h>
#include <pthread.h>
int test_pthread_create(pthread_t *, const pthread_attr_t *,
                        void *(*)(void *), void *);
int test_pthread_join(pthread_t, void **);
int test_pthread_attr_setstacksize(pthread_attr_t *, size_t);
mqd_t test_mq_open(const char *, int, ...);
int test_mq_close(mqd_t);
int test_mq_unlink(const char *);
int test_mq_send(mqd_t, const char *, size_t, unsigned);
ssize_t test_mq_receive(mqd_t, char *, size_t, unsigned *);
int test_mq_getattr(mqd_t, struct mq_attr *);
int test_poll(struct pollfd *, nfds_t, int);
#define pthread_create test_pthread_create
#define pthread_join test_pthread_join
#define pthread_attr_setstacksize test_pthread_attr_setstacksize
#define mq_open test_mq_open
#define mq_close test_mq_close
#define mq_unlink test_mq_unlink
#define mq_send test_mq_send
#define mq_receive test_mq_receive
#define mq_getattr test_mq_getattr
#define poll test_poll
"""

HARNESS = r"""
#define _POSIX_C_SOURCE 200809L
#include "runtime.h"
#include <nuttx/config.h>
#include <nuttx/sched.h>
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <mqueue.h>
#include <poll.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct context {
  void *(*entry)(void *);
  void *argument;
  pthread_t thread;
  struct tcb_s tcb;
  unsigned alive, captured, joined;
};
static struct context contexts[ES_SERVICES];
static struct tcb_s main_tcb = {8192}, idle_tcb = {3072};
static _Thread_local unsigned current = ES_SERVICES;
static unsigned attempts, created, joined, queue_opens, queue_closes;
static _Atomic unsigned captures;
static unsigned live_threads, live_queues, live_messages, queue_attempts;
struct mock_queue {
  unsigned alive, head, count, capacity;
  char name[64];
  struct es_event events[24];
};
static struct mock_queue queue_storage[ES_SERVICES * ES_QUEUES_PER_SERVICE];
static struct mock_queue *queue(mqd_t fd) {
  unsigned index = (unsigned)fd - 100u;
  assert(index < ES_SERVICES * ES_QUEUES_PER_SERVICE);
  assert(queue_storage[index].alive);
  return &queue_storage[index];
}

static int fault(const char *name, unsigned index) {
  const char *value = getenv(name);
  return value && (unsigned)strtoul(value, NULL, 10) == index;
}
static void *start(void *argument) {
  current = (unsigned)(uintptr_t)argument;
  struct context *ctx = &contexts[current];
  ctx->alive = 1;
  void *result = ctx->entry(ctx->argument);
#if ES_STACK_WATERMARK && !defined(MOCK_NO_COLORATION)
  assert(ctx->captured == 1); /* Must precede TCB/stack destruction. */
#endif
  ctx->alive = 0;
  return result;
}
int test_pthread_attr_setstacksize(pthread_attr_t *attr, size_t size) {
  assert(size == ES_STACK_BYTES);
  /* The POSIX host needs its own legal stack size, not the simulated target's. */
  return pthread_attr_setstacksize(attr, 65536);
}
int test_pthread_create(pthread_t *thread, const pthread_attr_t *attr,
                        void *(*entry)(void *), void *argument) {
  if (fault("FAIL_THREAD_AT", attempts++)) return EAGAIN;
  unsigned slot = (unsigned)(uintptr_t)argument;
  assert(slot < ES_SERVICES);
  contexts[slot] = (struct context){.entry = entry, .argument = argument,
                                  .tcb = {ES_STACK_BYTES}};
  int rc = pthread_create(thread, attr, start, argument);
  if (!rc) {
    contexts[slot].thread = *thread;
    ++created;
    ++live_threads;
  }
  return rc;
}
int test_pthread_join(pthread_t thread, void **result) {
  unsigned slot;
  for (slot = 0; slot < ES_SERVICES; slot++)
    if (!contexts[slot].joined && pthread_equal(thread, contexts[slot].thread)) break;
  assert(slot < ES_SERVICES);
  int rc = pthread_join(thread, result);
  if (!rc) {
    assert(!contexts[slot].alive);
    contexts[slot].joined = 1;
    --live_threads;
    ++joined;
  }
  return rc;
}
struct tcb_s *nxsched_self(void) {
  if (current == ES_SERVICES) return &main_tcb;
  assert(contexts[current].alive);
  return &contexts[current].tcb;
}
struct tcb_s *nxsched_get_tcb(int id) { assert(id == 0); return &idle_tcb; }
size_t up_check_tcbstack(struct tcb_s *tcb, size_t size) {
  assert(tcb->adj_stack_size == size);
  if (tcb == &main_tcb) return 1536;
  if (tcb == &idle_tcb) return 384;
  assert(current < ES_SERVICES && tcb == &contexts[current].tcb);
  assert(contexts[current].alive && !contexts[current].captured);
  contexts[current].captured = 1;
  ++captures;
  return 640 + current * 16;
}
size_t up_check_intstack(int cpu, size_t size) {
  assert(cpu == 0 && size == 4096);
  return 256;
}

mqd_t test_mq_open(const char *name, int flags, ...) {
  if (fault("FAIL_QUEUE_AT", queue_attempts++)) { errno = ENOMEM; return (mqd_t)-1; }
  va_list args;
  va_start(args, flags);
  mode_t mode = va_arg(args, mode_t);
  struct mq_attr *attr = va_arg(args, struct mq_attr *);
  va_end(args);
  (void)mode;
  assert(attr->mq_maxmsg == ES_QUEUE_SLOTS && attr->mq_msgsize == ES_EVENT_BYTES);
  for (unsigned i = 0; i < ES_SERVICES * ES_QUEUES_PER_SERVICE; i++) {
    if (!queue_storage[i].alive) {
      queue_storage[i] = (struct mock_queue){.alive = 1, .capacity = attr->mq_maxmsg};
      assert(strlen(name) < sizeof(queue_storage[i].name));
      strcpy(queue_storage[i].name, name);
      ++queue_opens;
      ++live_queues;
      return (mqd_t)(100 + i);
    }
  }
  errno = ENFILE;
  return (mqd_t)-1;
}
int test_mq_close(mqd_t fd) {
  struct mock_queue *q = queue(fd);
  assert(q->count == 0);
  q->alive = 0;
  ++queue_closes;
  --live_queues;
  return 0;
}
int test_mq_unlink(const char *name) {
  for (unsigned i = 0; i < ES_SERVICES * ES_QUEUES_PER_SERVICE; i++) {
    if (!strcmp(name, queue_storage[i].name)) {
      assert(!queue_storage[i].alive);
      queue_storage[i].name[0] = 0;
      return 0;
    }
  }
  assert(!"unlink without owned queue");
  return -1;
}
int test_mq_send(mqd_t fd, const char *data, size_t size, unsigned priority) {
  (void)priority;
  struct mock_queue *q = queue(fd);
  assert(size == sizeof(struct es_event));
  if (q->count == q->capacity) { errno = EAGAIN; return -1; }
  memcpy(&q->events[(q->head + q->count) % q->capacity], data, size);
  ++q->count;
  ++live_messages;
  return 0;
}
ssize_t test_mq_receive(mqd_t fd, char *data, size_t size, unsigned *priority) {
  (void)priority;
  struct mock_queue *q = queue(fd);
  assert(size == sizeof(struct es_event));
  if (!q->count) { errno = EAGAIN; return -1; }
  memcpy(data, &q->events[q->head], size);
  q->head = (q->head + 1) % q->capacity;
  --q->count;
  --live_messages;
  return (ssize_t)size;
}
int test_mq_getattr(mqd_t fd, struct mq_attr *attr) {
  struct mock_queue *q = queue(fd);
  *attr = (struct mq_attr){.mq_curmsgs = q->count, .mq_maxmsg = q->capacity,
                           .mq_msgsize = ES_EVENT_BYTES, .mq_flags = O_NONBLOCK};
  return 0;
}
int test_poll(struct pollfd *fds, nfds_t count, int timeout) {
  (void)timeout;
  unsigned ready = 0;
  for (unsigned i = 0; i < count; i++) {
    fds[i].revents = queue((mqd_t)fds[i].fd)->count ? POLLIN : 0;
    ready += fds[i].revents != 0;
  }
  return (int)ready;
}
struct mallinfo { int uordblks; };
struct mallinfo test_mallinfo(void) {
  /* Synthetic accounting makes thread-only/queue-only contamination observable. */
  return (struct mallinfo){4096 + live_threads * (ES_STACK_BYTES + 128) +
                          live_queues * 64 + live_messages * 80};
}
int es_hal_init(void) { return 0; }
int es_hal_apply(unsigned state) { (void)state; return 0; }
int es_hal_close(void) { return 0; }
int main(int argc, char **argv) {
  int failed = 0;
  for (int i = 1; i < argc; i++) {
    const char *command[] = {"es_c", argv[i]};
    int rc = es_run_main(2, command);
    printf("ES_COMMAND_EXIT status=%d\n", rc);
    failed |= rc != 0;
  }
  printf("MOCK_SUMMARY created=%u joined=%u captures=%u queue_opens=%u "
         "queue_closes=%u live_workers=%u live_queues=%u live_messages=%u\n",
         created, joined, captures, queue_opens, queue_closes,
         live_threads, live_queues, live_messages);
  return failed;
}
"""

ZEPHYR_KERNEL = r"""
#pragma once
#include <stddef.h>
#include <stdint.h>
#define CONFIG_MAIN_STACK_SIZE 8192
#define CONFIG_IDLE_STACK_SIZE 1024
#define CONFIG_ISR_STACK_SIZE 4096
#define CONFIG_SYSTEM_WORKQUEUE_STACK_SIZE 1024
#define CONFIG_THREAD_STACK_INFO 1
#define CONFIG_INIT_STACKS 1
#define CONFIG_THREAD_MONITOR 1
#define CONFIG_SYS_HEAP_RUNTIME_STATS 1
#define CONFIG_HEAP_MEM_POOL_SIZE 0
#define K_HEAP_MEM_POOL_SIZE 4096
#define K_IDLE_PRIO 16
#define K_FOREVER (-1)
#define K_NO_WAIT 0
#define K_MSEC(x) (x)
#define K_USEC(x) (x)
#define K_SECONDS(x) ((x) * 1000)
#define K_POLL_STATE_NOT_READY 0
#define K_POLL_STATE_MSGQ_DATA_AVAILABLE 1
#define K_POLL_TYPE_MSGQ_DATA_AVAILABLE 1
#define K_POLL_MODE_NOTIFY_ONLY 1
#define K_THREAD_STACK_ARRAY_DEFINE(name,n,bytes) char name[n][bytes]
#define K_THREAD_STACK_SIZEOF(stack) sizeof(stack)
#define K_SEM_DEFINE(name,initial,limit) struct k_sem name = {initial}
struct k_sem { unsigned count; };
struct k_thread { struct { size_t size; } stack_info; };
struct k_msgq { unsigned used; };
struct k_poll_event { unsigned state; };
struct k_work_q { struct k_thread thread; };
extern struct k_work_q k_sys_work_q;
typedef struct k_thread *k_tid_t;
typedef unsigned k_spinlock_key_t;
struct k_heap { unsigned lock, heap; };
void k_sem_reset(struct k_sem *);
int k_sem_take(struct k_sem *, int64_t);
void k_sem_give(struct k_sem *);
void k_msgq_init(struct k_msgq *, char *, size_t, unsigned);
void k_poll_event_init(struct k_poll_event *, unsigned, unsigned, void *);
int k_msgq_put(struct k_msgq *, const void *, int64_t);
unsigned k_msgq_num_used_get(struct k_msgq *);
int k_msgq_get(struct k_msgq *, void *, int64_t);
int k_poll(struct k_poll_event *, unsigned, int64_t);
uint32_t k_cycle_get_32(void);
int64_t k_uptime_get(void);
void k_sleep(int64_t);
k_tid_t k_thread_create(struct k_thread *, void *, size_t,
                       void (*)(void *,void *,void *), void *,void *,void *, int,unsigned,int64_t);
int k_thread_join(struct k_thread *, int64_t);
int k_thread_stack_space_get(const struct k_thread *, size_t *);
struct k_thread *k_current_get(void);
int k_thread_priority_get(struct k_thread *);
void k_thread_foreach(void (*)(const struct k_thread *, void *), void *);
unsigned irq_lock(void);
void irq_unlock(unsigned);
static inline k_spinlock_key_t k_spin_lock(unsigned *lock) { (void)lock; return 0; }
static inline void k_spin_unlock(unsigned *lock, k_spinlock_key_t key) { (void)lock;(void)key; }
"""

ZEPHYR_HEAP = r"""
#pragma once
#include <stddef.h>
struct sys_memory_stats { size_t allocated_bytes; };
static inline int sys_heap_runtime_stats_get(unsigned *heap, struct sys_memory_stats *stats) {
  (void)heap;
  stats->allocated_bytes = 777;
  return 0;
}
"""


def records(output, prefix):
    return [dict(field.split("=", 1) for field in line.split()[1:])
            for line in output.splitlines() if line.startswith(prefix + " ")]


class NativeRamControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = shutil.which("cc")
        if not cls.cc:
            raise unittest.SkipTest("C compiler required")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        for name, text in HEADERS.items():
            path = cls.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (cls.root / "harness.c").write_text(HARNESS)
        (cls.root / "mock_redirect.h").write_text(MOCK_REDIRECT)
        cls.binaries = {}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def executable(self, layout=0, watermark=1, stack=4096, coloration=1, fortify=0):
        key = (layout, watermark, stack, coloration, fortify)
        if key in self.binaries:
            return self.binaries[key]
        name = "-".join(map(str, key))
        obj, binary = self.root / (name + ".o"), self.root / name
        flags = [self.cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                 f"-DES_MAILBOX={layout}", f"-DES_STACK_WATERMARK={watermark}",
                 f"-DES_STACK_BYTES={stack}", "-I", str(self.root), "-I", str(HERE)]
        if not coloration:
            flags.append("-DMOCK_NO_COLORATION=1")
        if fortify:
            flags.extend(["-U_FORTIFY_SOURCE", f"-D_FORTIFY_SOURCE={fortify}"])
        subprocess.run([*flags, "-D__NuttX__", "-include", str(self.root / "mock_redirect.h"),
                        "-c", str(HERE / "platform_nuttx.c"), "-o", str(obj)],
                       check=True, capture_output=True, text=True)
        subprocess.run([*flags, str(self.root / "harness.c"), str(obj),
                        *[str(HERE / name) for name in ("runtime.c", "saturation.c", "core.c")],
                        "-lrt", "-o", str(binary)], check=True, capture_output=True, text=True)
        self.binaries[key] = binary
        return binary

    def run_case(self, *profiles, fault=None, **build):
        env = os.environ.copy()
        if fault:
            env.update(fault)
        return subprocess.run([str(self.executable(**build)), *profiles],
                              env=env, capture_output=True, text=True, timeout=20)

    def assert_clean(self, result):
        summary = records(result.stdout, "MOCK_SUMMARY")[0]
        self.assertEqual(summary["created"], summary["joined"], result.stdout)
        self.assertEqual(summary["queue_opens"], summary["queue_closes"], result.stdout)
        for name in ("live_workers", "live_queues", "live_messages"):
            self.assertEqual(summary[name], "0", result.stdout)
        return summary

    def test_thread_sweep_never_constructs_queues_and_captures_before_free(self):
        for count in (0, 1, 5, 10, 20):
            with self.subTest(count=count):
                result = self.run_case(f"alloc-threads-{count}")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                summary = self.assert_clean(result)
                self.assertEqual(summary["queue_opens"], "0")
                self.assertEqual(summary["captures"], str(count))
                phases = records(result.stdout, "ES_ALLOC")
                self.assertEqual([p["stage"] for p in phases],
                                 ["baseline", "threads", "threads-joined", "cleanup"])
                self.assertEqual(phases[1]["worker_stack_requested"], str(count * 4096))
                self.assertEqual(phases[1]["worker_stack_reserved"], "0")
                self.assertEqual(phases[1]["worker_stack_reservation_available"], "0")
                self.assertEqual(phases[1]["heap_used"], str(4096 + count * (4096 + 128)))
                self.assertEqual(phases[-1]["heap_used"], phases[0]["heap_used"])
                stacks = [r for r in records(result.stdout, "ES_STACK") if r["scope"] == "worker"]
                self.assertEqual(len(stacks), count)
                for index, row in enumerate(stacks):
                    self.assertEqual(row["used"], str(640 + index * 16))
                    self.assertEqual(row["available"], "1")
                    self.assertEqual(row["capture"], "before_thread_return")

    def test_native_output_satisfies_the_collector_contract_for_both_layouts(self):
        for layout, queues in ((0, 60), (1, 20)):
            for profile in contract.ALLOC_PROFILES:
                with self.subTest(layout=layout, profile=profile):
                    result = self.run_case(profile, layout=layout)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    parsed = contract.allocation_output(result.stdout, profile, "nuttx-c",
                                                         {"queues": queues, "worker_stack_bytes": 4096})
                    self.assertEqual(parsed["result"]["errors"], 0)

    def test_queue_control_matches_both_layouts_without_workers(self):
        for layout, queues in ((0, 60), (1, 20)):
            with self.subTest(layout=layout):
                result = self.run_case("alloc-queues", layout=layout)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                summary = self.assert_clean(result)
                self.assertEqual(summary["created"], "0")
                self.assertEqual(summary["queue_opens"], str(queues))
                phases = records(result.stdout, "ES_ALLOC")
                self.assertEqual([p["stage"] for p in phases],
                                 ["baseline", "queues-empty", "queues-full", "queues-drained", "cleanup"])
                self.assertEqual(int(phases[2]["heap_used"]) - int(phases[1]["heap_used"]), 480 * 80)
                self.assertEqual(phases[1]["heap_used"], phases[3]["heap_used"])
                self.assertEqual(phases[0]["heap_used"], phases[4]["heap_used"])

    def test_partial_spawn_failure_joins_survivors_and_next_invocation_recovers(self):
        result = self.run_case("alloc-threads-5", "alloc-threads-1", fault={"FAIL_THREAD_AT": "2"})
        self.assertNotEqual(result.returncode, 0)
        summary = self.assert_clean(result)
        self.assertEqual(summary["captures"], "3")
        results = records(result.stdout, "ES_ALLOC_RESULT")
        self.assertNotEqual(results[0]["errors"], "0")
        self.assertEqual(results[1]["errors"], "0")

    def test_fortified_system_headers_cannot_bypass_mock_queues_or_poll(self):
        for layout, queues in ((0, 60), (1, 20)):
            with self.subTest(layout=layout):
                result = self.run_case("alloc-queues", "saturation", layout=layout, fortify=3)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                summary = self.assert_clean(result)
                self.assertEqual(summary["queue_opens"], str(2 * queues))
                self.assertEqual(summary["captures"], "20")
                self.assertEqual(records(result.stdout, "ES_SAT_RESULT")[0]["errors"], "0")

    def test_partial_queue_failure_closes_only_owned_queues_and_can_retry(self):
        result = self.run_case("alloc-queues", "alloc-queues", fault={"FAIL_QUEUE_AT": "3"})
        self.assertNotEqual(result.returncode, 0)
        self.assert_clean(result)
        results = records(result.stdout, "ES_ALLOC_RESULT")
        self.assertNotEqual(results[0]["errors"], "0")
        self.assertEqual(results[1]["errors"], "0")

    def test_watermark_disabled_or_unsupported_is_explicit_and_stacks_configure(self):
        for options in ({"watermark": 0}, {"coloration": 0}):
            with self.subTest(options=options):
                result = self.run_case("alloc-threads-1", stack=2048, **options)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_clean(result)
                worker = next(r for r in records(result.stdout, "ES_STACK") if r["scope"] == "worker")
                self.assertEqual((worker["reserved"], worker["used"], worker["available"]),
                                 ("2048", "0", "0"))

    def test_saturation_uses_configured_stacks_and_observes_all_workers(self):
        result = self.run_case("saturation", stack=2048)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.assert_clean(result)["captures"], "20")
        self.assertEqual(records(result.stdout, "ES_SAT_RESULT")[0]["stacks"], "40960")

    def test_zephyr_counts_static_capacity_and_board_added_heap_at_baseline(self):
        directory = self.root / "zephyr-probe"
        directory.mkdir(exist_ok=True)
        for name, contents in (("zephyr/kernel.h", ZEPHYR_KERNEL),
                               ("zephyr/sys/sys_heap.h", ZEPHYR_HEAP)):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        driver = directory / "driver.c"
        driver.write_text('#include "platform.h"\n#include <zephyr/kernel.h>\n'
                          'struct k_heap _system_heap;\n'
                          'int main(void) { es_platform_alloc_report("baseline", 0); return 0; }\n')
        for watermark in (0, 1):
            with self.subTest(watermark=watermark):
                binary = directory / f"baseline-{watermark}"
                result = subprocess.run(
                    [self.cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                     "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
                     f"-DES_STACK_WATERMARK={watermark}", "-DES_STACK_BYTES=2048",
                     "-I", str(directory), "-I", str(HERE),
                     str(HERE / "platform_zephyr.c"), str(driver), "-o", str(binary)],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([str(binary)], capture_output=True, text=True, check=True)
                row = records(result.stdout, "ES_ALLOC")[0]
                self.assertEqual(row["heap_used"], "777")
                self.assertEqual(row["heap_arena_reserved"], "4096")
                self.assertEqual(row["available"], "1")
                self.assertEqual(row["live_workers"], "0")
                self.assertEqual(row["active_queues"], "0")
                self.assertEqual(row["worker_stack_reserved"], "40960")
                self.assertEqual(row["worker_stack_requested"], "0")
                self.assertEqual(row["worker_stack_reservation_available"], "1")
                self.assertEqual(row["queue_payload_capacity"], "30720")
                self.assertGreater(int(row["static_storage"]), 40960 + 30720)


if __name__ == "__main__":
    unittest.main()
