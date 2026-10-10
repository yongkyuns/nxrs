/* ESP32-S3 compiler diagnosis only; not a service or production HAL.
 * The generated table contains independently linked copies of compiler
 * output at different offsets, without changing their instructions. */
#include <nuttx/config.h>
#include <nuttx/irq.h>
#include <nuttx/atomic.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#if CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ != 240 || defined(CONFIG_SMP)
#  error "Probe requires the matched single-core 240 MHz configuration"
#endif

typedef uint32_t (*probe_fn)(uint32_t, uint32_t, uint32_t);
struct probe_case { const char *name; probe_fn fn; };
#include "compiler-probe-table.h"

static volatile uint32_t sink;
static atomic_t competitor_stop;
static atomic_t competitor_jobs;

static uint32_t cycles(void) {
  uint32_t value;
  __asm__ volatile("rsr.ccount %0" : "=a"(value) : : "memory");
  return value;
}

static void *compete(void *unused) {
  (void)unused;
  while (!atomic_read(&competitor_stop)) {
    /* A controlled scheduling competitor, not an RTOS-service simulation. */
    uint32_t end = cycles() + 24000;
    while ((int32_t)(cycles() - end) < 0) { }
    atomic_fetch_add_relaxed(&competitor_jobs, 1);
    usleep(1000);
  }
  return NULL;
}

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "short";
  int short_test = !strcmp(mode, "short");
  int locked = !strcmp(mode, "locked");
  int concurrent = !strcmp(mode, "concurrent");
  if (!short_test && !locked && !concurrent && strcmp(mode, "normal"))
    return 2;
  uint32_t iterations = short_test ? 512 : 400000;
  unsigned repeats = short_test ? 9 : 3;
  pthread_t competitor;
  atomic_set(&competitor_stop, 0);
  atomic_set(&competitor_jobs, 0);
  if (concurrent) {
    int error = pthread_create(&competitor, NULL, compete, NULL);
    if (error) return 3;
  }
  unsigned errors = 0;
  for (unsigned repeat = 0; repeat < repeats; ++repeat) {
    for (unsigned index = 0; index < PROBE_CASE_COUNT; ++index) {
      /* Rotate order and verify every result against the same GCC control. */
      unsigned selected = (index + repeat) % PROBE_CASE_COUNT;
      const struct probe_case *test = &probe_cases[selected];
      uint32_t seed = UINT32_C(0x12345678) + repeat;
      uint32_t token = UINT32_C(0x87654321) ^ repeat;
      uint32_t expected = probe_cases[0].fn(seed, token, iterations);
      sink = test->fn(seed, token, 32); /* warm the function's instruction cache */
      irqstate_t flags = 0;
      if (short_test) flags = enter_critical_section();
      if (locked) sched_lock(); /* interrupts stay enabled */
      uint32_t before = cycles();
      uint32_t result = test->fn(seed, token, iterations);
      uint32_t elapsed = cycles() - before;
      if (locked) sched_unlock();
      if (short_test) leave_critical_section(flags);
      sink = result;
      errors += result != expected;
      printf("CP_ROW mode=%s repeat=%u case=%s address=%lu iterations=%lu cycles=%lu result=%lu expected=%lu\n",
             mode, repeat, test->name, (unsigned long)(uintptr_t)test->fn,
             (unsigned long)iterations, (unsigned long)elapsed,
             (unsigned long)result, (unsigned long)expected);
    }
  }
  if (concurrent) {
    atomic_set(&competitor_stop, 1);
    pthread_join(competitor, NULL);
  }
  printf("CP_DONE mode=%s cases=%u repeats=%u errors=%u competitor_jobs=%lu\n",
         mode, PROBE_CASE_COUNT, repeats, errors,
         (unsigned long)atomic_read(&competitor_jobs));
  return errors ? 1 : 0;
}
