/* SPDX-License-Identifier: MIT
 * Diagnostic-only bounded consumer stalls and real queue-full accounting.
 * Successful source pacing is suppressed; EAGAIN retry sleeps are preserved.
 * No hooks are linked into the ordinary footprint/latency images. */
#include "qualification.h"
#include <errno.h>
#include <limits.h>
#include <mqueue.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#ifdef __NuttX__
#include <malloc.h>
#include <nuttx/mm/mm.h>
#endif

int __real_nxrs_sq_run(int, char **);
int __real_sem_init(sem_t *, int, unsigned);
int __real_sem_post(sem_t *);
int __real_usleep(useconds_t);
int __real_mq_send(mqd_t, const char *, size_t, unsigned);
int __real_mq_close(mqd_t);
int __real_mq_unlink(const char *);
int __real_nxrs_cq_thread_join(void *, void **);
int __real_nxrs_sq_wait(struct sq_service *, const struct sq_event *, struct sq_event *);
void __real_nxrs_sq_record(struct sq_service *, const struct sq_event *);

enum { NORMAL, LOAD, CANCEL, EVENTS = 2048, REPETITIONS = 3, GATE_EVERY = 64,
       GATE_US = 20000 };
static unsigned mode, sems, skipped, closed, unlinked, joined, last_gate;
static int source_sent;
static sem_t *go;
static pthread_t coordinator;
static atomic_uint active, armed, source_ok, forward_ok, source_full, forward_full,
                   control_full, data_full, terminal_received, gates, cancelled,
                   stop_sent, stop_full;

static int owner(void) { return pthread_equal(pthread_self(), coordinator); }
static unsigned heap_used(void) {
#ifdef __NuttX__
  return mallinfo().uordblks;
#else
  return 0; /* Host checks never claim target RAM. */
#endif
}
static uint64_t milliseconds(void) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return UINT64_MAX;
  return (uint64_t)now.tv_sec * 1000u + now.tv_nsec / 1000000u;
}

int __wrap_sem_init(sem_t *sem, int shared, unsigned value) {
  int result = __real_sem_init(sem, shared, value);
  /* Frozen runtime creates ready then go, before releasing any worker. Fail
   * qualification if that two-gate contract changes; never count its initial
   * all-queues-full probe as traffic pressure. */
  if (atomic_load(&active) && result == 0 && owner() && ++sems == 2) go = sem;
  return result;
}
int __wrap_sem_post(sem_t *sem) {
  if (atomic_load(&active) && owner() && sem == go) atomic_store_explicit(&armed, 1, memory_order_release);
  return __real_sem_post(sem);
}
int __wrap_usleep(useconds_t delay) {
  if (atomic_load(&active) && owner() && mode != NORMAL && delay == 100 && source_sent) {
    source_sent = 0;
    skipped++;
    return 0;
  }
  return __real_usleep(delay);
}
int __wrap_mq_send(mqd_t descriptor, const char *message, size_t length, unsigned priority) {
  int result = __real_mq_send(descriptor, message, length, priority);
  int saved = errno;
  if (atomic_load_explicit(&armed, memory_order_acquire) && length == sizeof(struct sq_event)) {
    unsigned kind = ((const struct sq_event *)message)->kind;
    if (kind == SQ_STOP) {
      if (result == 0) atomic_fetch_add(&stop_sent, 1);
      else if (saved == EAGAIN) atomic_fetch_add(&stop_full, 1);
    } else if (kind == SQ_CONTROL || kind == SQ_DATA) {
      int producer = owner();
      if (producer) source_sent = result == 0;
      if (result == 0) atomic_fetch_add(producer ? &source_ok : &forward_ok, 1);
      else if (saved == EAGAIN) {
        atomic_fetch_add(producer ? &source_full : &forward_full, 1);
        atomic_fetch_add(kind == SQ_CONTROL ? &control_full : &data_full, 1);
        if (producer && mode == CANCEL) {
          /* Cancel only AFTER a real queue-full response. This intentional
           * abort must not be counted as loss-free business delivery. */
          atomic_store(&cancelled, 1);
          result = -1;
          saved = EIO;
        }
      }
    }
  }
  errno = saved;
  return result;
}
int __wrap_nxrs_sq_wait(struct sq_service *service, const struct sq_event *pending,
                         struct sq_event *event) {
  if (atomic_load(&active) && mode != NORMAL && nxrs_sq_id(service) == nxrs_sq_count(service) - 1) {
    unsigned received = atomic_load(&terminal_received);
    if (received < EVENTS && received % GATE_EVERY == 0 && received != last_gate) {
      last_gate = received;
      atomic_fetch_add(&gates, 1);
      if (__real_usleep(GATE_US) != 0) return -1;
    }
  }
  return __real_nxrs_sq_wait(service, pending, event);
}
void __wrap_nxrs_sq_record(struct sq_service *service, const struct sq_event *event) {
  __real_nxrs_sq_record(service, event);
  if (atomic_load(&active) && nxrs_sq_id(service) == nxrs_sq_count(service) - 1)
    atomic_fetch_add(&terminal_received, 1);
}
int __wrap_mq_close(mqd_t descriptor) {
  int status = __real_mq_close(descriptor);
  if (atomic_load(&active) && owner() && status == 0) closed++;
  return status;
}
int __wrap_mq_unlink(const char *name) {
  int status = __real_mq_unlink(name);
  if (atomic_load(&active) && owner() && status == 0) unlinked++;
  return status;
}
int __wrap_nxrs_cq_thread_join(void *thread, void **result) {
  int status = __real_nxrs_cq_thread_join(thread, result);
  if (atomic_load(&active) && owner() && status == 0) joined++;
  return status;
}

static int call(const char *phase, unsigned index, unsigned selected, unsigned baseline) {
  mode = selected;
  sems = skipped = closed = unlinked = joined = 0;
  go = NULL;
  last_gate = UINT_MAX;
  source_sent = 0;
  atomic_store(&armed, 0);
  atomic_store(&source_ok, 0); atomic_store(&forward_ok, 0);
  atomic_store(&source_full, 0); atomic_store(&forward_full, 0);
  atomic_store(&control_full, 0); atomic_store(&data_full, 0);
  atomic_store(&terminal_received, 0); atomic_store(&gates, 0);
  atomic_store(&cancelled, 0); atomic_store(&stop_sent, 0); atomic_store(&stop_full, 0);
  atomic_store(&active, 1);
  char *arguments[] = {"sq", "20", selected == NORMAL ? "100" : "2048",
                       selected == NORMAL ? "2000" : "100", NULL};
  printf("SQ_PRESSURE_BEGIN phase=%s index=%u\n", phase, index);
  uint64_t begin = milliseconds();
  if (begin == UINT64_MAX) { atomic_store(&active, 0); return 0; }
  int status = __real_nxrs_sq_run(4, arguments);
  unsigned heap_before_drain = heap_used();
#ifdef __NuttX__
  /* Exit context cannot safely release its running stack/TCB. NuttX queues
   * these frees until mm_malloc drains them; sleeping/mallinfo does not.
   * Observe both snapshots, then drain only after every real join succeeded.
   * This is diagnostic accounting, not production GC or a leak allowance. */
  if (joined == 20) mm_free_delaylist(USR_HEAP);
#endif
  uint64_t end = milliseconds();
  unsigned heap = heap_used();
  unsigned reclaimed = heap <= heap_before_drain ? heap_before_drain - heap : UINT_MAX;
  unsigned elapsed_ms = end == UINT64_MAX || end < begin || end - begin > UINT_MAX
                        ? UINT_MAX : (unsigned)(end - begin);
  printf("SQ_PRESSURE_END phase=%s index=%u source_ok=%u forward_ok=%u source_full=%u forward_full=%u control_full=%u data_full=%u terminal=%u gates=%u skipped=%u cancelled=%u sems=%u stop_sent=%u stop_full=%u joined=%u closed=%u unlinked=%u heap_before_drain=%u deferred_reclaimed=%u heap=%u elapsed_ms=%u status=%d\n",
         phase, index, atomic_load(&source_ok), atomic_load(&forward_ok),
         atomic_load(&source_full), atomic_load(&forward_full), atomic_load(&control_full),
         atomic_load(&data_full), atomic_load(&terminal_received), atomic_load(&gates), skipped,
         atomic_load(&cancelled), sems, atomic_load(&stop_sent), atomic_load(&stop_full),
         joined, closed, unlinked, heap_before_drain, reclaimed, heap, elapsed_ms, status);
  atomic_store(&armed, 0);
  atomic_store(&active, 0);
  if (sems != 2 || joined != 20 || closed != 60 || unlinked != 60 ||
      atomic_load(&stop_full) != 0 || reclaimed == UINT_MAX || elapsed_ms >= 10000 ||
      (baseline && heap != baseline)) return 0;
  if (selected == CANCEL)
    return status == 1 && atomic_load(&cancelled) == 1 && atomic_load(&source_full) == 1 &&
           atomic_load(&source_ok) < EVENTS && atomic_load(&stop_sent) > 0;
  unsigned events = selected == NORMAL ? 100 : EVENTS;
  if (status != 0 || atomic_load(&source_ok) != events ||
      atomic_load(&forward_ok) != events * 19 || atomic_load(&terminal_received) != events) return 0;
  return selected == NORMAL || (atomic_load(&source_full) > 0 && atomic_load(&forward_full) > 0 &&
         atomic_load(&gates) == EVENTS / GATE_EVERY && skipped == EVENTS);
}

int __wrap_nxrs_sq_run(int argc, char **argv) {
  if (argc != 2 || strcmp(argv[1], "pressure") != 0) return 2;
  coordinator = pthread_self();
  if (!call("warmup", 0, NORMAL, 0)) goto failed;
  unsigned baseline = heap_used();
  printf("SQ_PRESSURE_BASELINE heap=%u\n", baseline);
  for (unsigned index = 0; index < REPETITIONS; index++)
    if (!call("load", index, LOAD, baseline) || !call("cancel", index, CANCEL, baseline) ||
        !call("recovery", index, NORMAL, baseline)) goto failed;
  printf("SQ_PRESSURE_BATCH repetitions=3 calls=10 expected_cancellations=3 verified_events=6544 heap_before=%u heap_after=%u status=0\n",
         baseline, heap_used());
  return 0;
failed:
  puts("SQ_PRESSURE_ERROR batch_failed");
  return 1;
}
