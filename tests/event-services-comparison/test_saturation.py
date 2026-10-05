"""Host-mock regression coverage for the all-slots-filled C qualification."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent

MOCK_PLATFORM = r"""\
#define _POSIX_C_SOURCE 200809L
#include "platform.h"
#include "saturation.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PHYSICAL_CAPACITY (ES_QUEUE_SLOTS + 1)
struct mock_queue {
  struct es_event items[PHYSICAL_CAPACITY];
  unsigned used, head;
};
static struct mock_queue queues[ES_SERVICES][ES_QUEUES_PER_SERVICE];
static pthread_t threads[ES_SERVICES];
static void *(*entries[ES_SERVICES])(void *);
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t condition = PTHREAD_COND_INITIALIZER;
static unsigned spawned, ready, cursor[ES_SERVICES];
static int released, initialized, joined, closed;
static unsigned corrupted, overflow_accepted;
static const char *fault;

static unsigned qindex(unsigned kind) { return ES_MAILBOX ? 0u : kind; }

static void *thread_start(void *arg) {
  unsigned index = (unsigned)(uintptr_t)arg;
  return entries[index](arg);
}

int es_platform_init(void) {
  memset(queues, 0, sizeof(queues));
  memset(cursor, 0, sizeof(cursor));
  spawned = ready = released = joined = closed = corrupted = 0;
  overflow_accepted = 0;
  fault = getenv("ES_SAT_FAULT");
  initialized = 1;
  return 0;
}

int es_platform_spawn(void *(*entry)(void *)) {
  if (!initialized || spawned == ES_SERVICES) return -1;
  unsigned index = spawned;
  entries[index] = entry;
  if (pthread_create(&threads[index], NULL, thread_start,
                     (void *)(uintptr_t)index) != 0) return -1;
  spawned++;
  return 0;
}

int es_platform_arrive(void) {
  pthread_mutex_lock(&lock);
  ready++;
  pthread_cond_broadcast(&condition);
  while (!released) pthread_cond_wait(&condition, &lock);
  pthread_mutex_unlock(&lock);
  return 0;
}

int es_platform_wait_ready(void) {
  pthread_mutex_lock(&lock);
  while (ready != spawned) pthread_cond_wait(&condition, &lock);
  pthread_mutex_unlock(&lock);
  return 0;
}

int es_platform_release(uint32_t *epoch) {
  if (epoch) *epoch = 1;
  pthread_mutex_lock(&lock);
  released = 1;
  pthread_cond_broadcast(&condition);
  pthread_mutex_unlock(&lock);
  return 0;
}

int es_platform_join(void) {
  (void)es_platform_release(NULL);
  for (unsigned i = 0; i < spawned; i++) pthread_join(threads[i], NULL);
  joined = 1;
  spawned = 0;
  return 0;
}

void es_platform_close(void) {
  if (!joined) (void)es_platform_join();
  initialized = 0;
  closed = 1;
  printf("MOCK_CLEANUP released=%d joined=%d closed=%d\n",
         released, joined, closed);
}

int es_platform_send(unsigned destination, unsigned kind,
                     const struct es_event *event) {
  if (!initialized || destination >= ES_SERVICES || kind >= ES_KINDS)
    return -1;
  struct mock_queue *queue = &queues[destination][qindex(kind)];
  if (queue->used >= ES_QUEUE_SLOTS) {
    if (fault && !strcmp(fault, "overflow") && !overflow_accepted &&
        queue->used < PHYSICAL_CAPACITY) {
      overflow_accepted = 1;
      unsigned tail = (queue->head + queue->used) % PHYSICAL_CAPACITY;
      queue->items[tail] = *event;
      queue->used++;
      return 0;
    }
    return 1;
  }
  unsigned tail = (queue->head + queue->used) % PHYSICAL_CAPACITY;
  queue->items[tail] = *event;
  queue->used++;
  return 0;
}

int es_platform_wait(unsigned service, uint32_t timeout,
                     struct es_event *event) {
  (void)timeout;
  if (!initialized || service >= ES_SERVICES) return -1;
  for (unsigned offset = 0; offset < ES_QUEUES_PER_SERVICE; offset++) {
    unsigned q = (cursor[service] + offset) % ES_QUEUES_PER_SERVICE;
    struct mock_queue *queue = &queues[service][q];
    if (!queue->used) continue;
    *event = queue->items[queue->head];
    queue->head = (queue->head + 1) % PHYSICAL_CAPACITY;
    queue->used--;
    cursor[service] = (q + 1) % ES_QUEUES_PER_SERVICE;
    if (fault && !strcmp(fault, "corrupt") && !corrupted++)
      event->payload[0] ^= 0xff;
    return 1;
  }
  return 0;
}

int es_platform_queue_depth(unsigned destination, unsigned kind) {
  if (!initialized || destination >= ES_SERVICES || kind >= ES_KINDS)
    return -1;
  int used = (int)queues[destination][qindex(kind)].used;
  if (fault && !strcmp(fault, "missing") && destination == 0 && kind == 0 &&
      used == ES_QUEUE_SLOTS) return used - 1;
  return used;
}

uint32_t es_platform_cycles(void) { return 1; }
uint32_t es_platform_heap_used(void) {
  unsigned used = 0;
  for (unsigned s = 0; s < ES_SERVICES; s++)
    for (unsigned q = 0; q < ES_QUEUES_PER_SERVICE; q++)
      used += queues[s][q].used;
  return used * ES_EVENT_BYTES;
}
unsigned es_platform_queue_peak(void) { return ES_QUEUE_SLOTS; }
void es_platform_resources(void) {
  printf("MOCK_RESOURCES held=%u\n", ready == ES_SERVICES && !released);
}
const char *es_platform_name(void) { return "mock"; }
int main(void) { return es_run_saturation(); }
"""


class SaturationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        if not cls.cc:
            raise unittest.SkipTest(
                "a C compiler is required for saturation mock tests"
            )

    def run_case(self, layout, fault=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        mock = root / "mock_platform.c"
        executable = root / "saturation"
        mock.write_text(MOCK_PLATFORM)
        subprocess.run(
            [
                self.cc,
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-pthread",
                f"-DES_MAILBOX={layout}",
                "-I",
                str(HERE),
                str(HERE / "saturation.c"),
                str(HERE / "core.c"),
                str(mock),
                "-o",
                str(executable),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        import os

        env = os.environ.copy()
        if fault:
            env["ES_SAT_FAULT"] = fault
        result = subprocess.run(
            [str(executable)], env=env, capture_output=True, text=True, timeout=15
        )
        return result

    def test_three_queue_and_mailbox_cycles_fill_overflow_and_drain(self):
        for layout, queues in ((0, 60), (1, 20)):
            with self.subTest(layout=layout):
                result = self.run_case(layout)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                cycle_rows = [
                    line
                    for line in result.stdout.splitlines()
                    if line.startswith("ES_SAT_CYCLE ")
                ]
                self.assertEqual(len(cycle_rows), 3)
                for cycle, row in enumerate(cycle_rows):
                    values = dict(field.split("=", 1) for field in row.split()[1:])
                    self.assertEqual(values["cycle"], str(cycle))
                    self.assertEqual(values["empty_heap"], "0")
                    self.assertEqual(values["full_heap"], "30720")
                    self.assertEqual(values["drained_heap"], "0")
                    self.assertEqual(values["filled"], "480")
                    self.assertEqual(values["drained"], "480")
                    self.assertEqual(values["overflow_rejected"], str(queues))
                    self.assertEqual(values["depth_full"], str(queues))
                    self.assertEqual(values["depth_zero"], str(queues))
                    self.assertEqual(values["errors"], "0")
                self.assertIn(
                    f"ES_SAT_RESULT platform=mock queues={queues} ", result.stdout
                )
                self.assertIn(
                    "slots=480 event_bytes=64 cycles=3 errors=0 stacks=81920",
                    result.stdout,
                )
                self.assertIn("MOCK_RESOURCES held=1", result.stdout)
                self.assertEqual(result.stdout.count("MOCK_RESOURCES held=1"), 3)
                self.assertIn(
                    "MOCK_CLEANUP released=1 joined=1 closed=1", result.stdout
                )
                self.assertTrue(result.stdout.rstrip().endswith("ES_PASS"))

    def test_missing_slot_is_detected_and_workers_are_cleaned_up(self):
        result = self.run_case(0, "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depth_full=59", result.stdout)
        self.assertIn("MOCK_CLEANUP released=1 joined=1 closed=1", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("ES_FAIL stage=saturation"))

    def test_accepted_overflow_is_detected_and_leftover_event_is_reported(self):
        result = self.run_case(0, "overflow")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("overflow_rejected=59", result.stdout)
        self.assertIn("depth_zero=59", result.stdout)
        self.assertIn("MOCK_CLEANUP released=1 joined=1 closed=1", result.stdout)

    def test_corrupt_drain_is_detected_and_workers_are_cleaned_up(self):
        result = self.run_case(1, "corrupt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ES_SAT_CYCLE cycle=0", result.stdout)
        self.assertIn("MOCK_CLEANUP released=1 joined=1 closed=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
