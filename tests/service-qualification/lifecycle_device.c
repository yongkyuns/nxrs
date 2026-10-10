/* SPDX-License-Identifier: MIT
 * Diagnostic coordinator: identical shutdown faults on Linux and NuttX.
 * All retries stay in this task, which owns the native descriptor table.
 * GNU link wrapping keeps these hooks out of normal application images. */
#include "qualification.h"
#include "lifecycle_faults.h"
#include <fcntl.h>
#include <mqueue.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#ifdef __NuttX__
#include <malloc.h>
#endif

int __real_nxrs_sq_run(int, char **);
mqd_t __real_mq_open(const char *, int, ...);
int __real_mq_unlink(const char *);
int __real_nxrs_cq_thread_start(void **, size_t, void *(*)(void *), void *);
uint32_t es_platform_cycles(void);

static unsigned opened, unlinked, started;
enum { REPETITIONS = 3 };

static unsigned heap_used(void) {
#ifdef __NuttX__
  return mallinfo().uordblks;
#else
  return 0; /* Host functional checks make no target heap claim. */
#endif
}

mqd_t __wrap_mq_open(const char *name, int flags, ...) {
  mqd_t descriptor;
  if (flags & O_CREAT) {
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = va_arg(arguments, mode_t);
    struct mq_attr *attributes = va_arg(arguments, struct mq_attr *);
    va_end(arguments);
    descriptor = __real_mq_open(name, flags, mode, attributes);
  } else {
    descriptor = __real_mq_open(name, flags);
  }
  if (descriptor != (mqd_t)-1) opened++;
  return descriptor;
}

int __wrap_mq_unlink(const char *name) {
  int status = __real_mq_unlink(name);
  if (status == 0) unlinked++;
  return status;
}

int __wrap_nxrs_cq_thread_start(void **handle, size_t stack,
                               void *(*entry)(void *), void *argument) {
  int status = __real_nxrs_cq_thread_start(handle, stack, entry, argument);
  if (status == 0) started++;
  return status;
}

static int call(int expected, int retained, unsigned baseline) {
  char *arguments[] = {"sq", "20", "100", "100", NULL};
  uint32_t begin = es_platform_cycles();
  int status = __real_nxrs_sq_run(4, arguments);
  unsigned descriptors = opened - sq_fault_closed();
  unsigned handles = started - sq_fault_joined();
  unsigned names = opened - unlinked;
  /* Thread teardown can finish just after join returns. Never hide positive
   * growth: allow at most one second to reach the declared warm baseline. */
  if (!retained)
    for (unsigned attempt = 0; heap_used() != baseline && attempt < 100; attempt++)
      usleep(10000);
  unsigned heap = heap_used();
  unsigned elapsed_ms = (es_platform_cycles() - begin) / 240000u;
  printf("SQ_OWNED descriptors=%u handles=%u names=%u heap=%u fault_hits=%u elapsed_ms=%u\n",
         descriptors, handles, names, heap, sq_fault_hits(), elapsed_ms);
  return status == expected && descriptors == (retained ? 60u : 0u) &&
         handles == (retained ? 1u : 0u) && names == (retained ? 60u : 0u) &&
         (retained ? heap >= baseline : heap == baseline) && elapsed_ms < 2000;
}

static int profile(unsigned kind, const char *scenario, unsigned baseline) {
  const unsigned worker_positions[] = {0, 7, 19};
  const unsigned close_positions[] = {0, 1, 30, 59};
  const unsigned *positions = kind == SQ_FAULT_CLOSE ? close_positions : worker_positions;
  unsigned length = kind == SQ_FAULT_CLOSE ? 4u : 3u;
  unsigned calls = 0, failures = 0, recoveries = 0;
  printf("SQ_DEVICE_BEGIN scenario=%s\n", scenario);
  for (unsigned repetition = 0; repetition < REPETITIONS; repetition++) {
    for (unsigned index = 0; index < length; index++) {
      unsigned position = positions[index];
      int retained = kind != SQ_FAULT_CLOSE;
      sq_fault_arm(kind, position);
      printf("SQ_DEVICE_CASE position=%u stage=failed\n", position);
      if (!call(1, retained, baseline) || sq_fault_hits() == 0) goto failed;
      calls++; failures++;
      if (retained) {
        printf("SQ_DEVICE_CASE position=%u stage=retry\n", position);
        if (!call(1, 1, baseline) || sq_fault_hits() < 2) goto failed;
        calls++; failures++;
      } else if (sq_fault_hits() != 1) goto failed;
      sq_fault_disarm();
      printf("SQ_DEVICE_CASE position=%u stage=recovery\n", position);
      if (!call(0, 0, baseline)) goto failed;
      calls++; recoveries++;
    }
  }
  printf("SQ_DEVICE_FAULT scenario=%s repetitions=%u calls=%u failures=%u recoveries=%u descriptors=0 handles=0 names=0 heap_growth=0\n",
         scenario, REPETITIONS, calls, failures, recoveries);
  return 1;
failed:
  /* Best-effort same-owner cleanup; the host's restore/reset safeguard also
   * covers fixture failure or timeout. Do not claim this as a passing run. */
  sq_fault_disarm();
  char *arguments[] = {"sq", "20", "100", "100", NULL};
  (void)__real_nxrs_sq_run(4, arguments);
  puts("SQ_DEVICE_ERROR profile_failed");
  return 0;
}

int __wrap_nxrs_sq_run(int argc, char **argv) {
  if (argc != 2 || strcmp(argv[1], "faults") != 0) return 2;
  sq_fault_disarm();
  puts("SQ_DEVICE_WARMUP");
  char *arguments[] = {"sq", "20", "100", "100", NULL};
  if (__real_nxrs_sq_run(4, arguments) != 0 ||
      opened != sq_fault_closed() || started != sq_fault_joined() || opened != unlinked) {
    puts("SQ_DEVICE_ERROR warmup_failed");
    return 1;
  }
  unsigned baseline = heap_used();
  printf("SQ_DEVICE_BASELINE heap=%u\n", baseline);
  if (!profile(SQ_FAULT_STOP, "fail-stop", baseline) ||
      !profile(SQ_FAULT_JOIN, "fail-join", baseline) ||
      !profile(SQ_FAULT_CLOSE, "fail-close", baseline)) return 1;
  printf("SQ_DEVICE_BATCH repetitions=%u calls=%u failures=%u recoveries=%u heap_before=%u heap_after=%u status=0\n",
         REPETITIONS, 1u + REPETITIONS * 26u, REPETITIONS * 16u,
         REPETITIONS * 10u, baseline, heap_used());
  return 0;
}

#ifndef __NuttX__
int main(int argc, char **argv) { return __wrap_nxrs_sq_run(argc, argv); }
#endif
