/* SPDX-License-Identifier: MIT
 * Diagnostic-only whole-run counters. No per-event instrumentation.
 * Register layout and selectors follow Espressif's xtensa_perfmon_access.c,
 * xtensa-debug-module.h and xt_perf_consts.h (ESP-IDF v5.4.2).
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <nuttx/config.h>
#include "qualification.h"

#if !defined(__XTENSA__) || defined(CONFIG_SMP)
#error "This diagnostic requires a single-core Xtensa NuttX build"
#endif

#define SQ_IRAM __attribute__((section(".iram1.sq_perfmon")))
#define SQ_PGM 0x101000u
#define SQ_PM0 0x101080u
#define SQ_CTRL0 0x101100u
#define SQ_STAT0 0x101180u

static inline uint32_t read_eri(uint32_t address) {
  uint32_t value;
  __asm__ volatile("rer %0, %1" : "=r"(value) : "r"(address) : "memory");
  return value;
}
static inline void write_eri(uint32_t address, uint32_t value) {
  __asm__ volatile("wer %0, %1\nmemw" :: "r"(value), "r"(address) : "memory");
}

static struct {
  const char *mode;
  uint32_t pgm, controls[2], counters[2], select, mask;
  unsigned active;
} state;

static SQ_IRAM int start_counters(void) {
  const char *requested = getenv("SQ_PM_MODE");
  const char *mode = requested ? requested : "fetch";
  /* Includes instruction RAM/ROM busy: the S3's SPI cache is external to LX7. */
  uint32_t select = 4, mask = 0x23;
  if (strcmp(mode, "all") == 0) mask = 0x1ff;
  else if (strcmp(mode, "data") == 0) { select = 3; mask = 0x1fe; }
  else if (strcmp(mode, "instructions") == 0) { select = 2; mask = 0x8dff; }
  else if (strcmp(mode, "fetch") != 0) return 2;
  uint32_t pgm = read_eri(SQ_PGM), controls[2], counters[2];
  if (pgm & 1u) return 2; /* Never take counters away from another profiler. */
  for (unsigned id = 0; id < 2; id++) {
    controls[id] = read_eri(SQ_CTRL0 + 4u * id);
    counters[id] = read_eri(SQ_PM0 + 4u * id);
    if (read_eri(SQ_STAT0 + 4u * id) & 1u) return 2;
  }
  /* TRACELEVEL=15, KRNLCNT=0 counts all interrupt levels; no overflow IRQ. */
  state.mode = mode;
  state.pgm = pgm;
  state.select = select;
  state.mask = mask;
  for (unsigned id = 0; id < 2; id++) {
    state.controls[id] = controls[id];
    state.counters[id] = counters[id];
  }
  write_eri(SQ_CTRL0, (mask << 16) | (select << 8) | 0xf0u);
  write_eri(SQ_CTRL0 + 4u, (0x20u << 16) | (5u << 8) | 0xf0u);
  write_eri(SQ_PM0, 0);
  write_eri(SQ_PM0 + 4u, 0);
  state.active = 1;
  write_eri(SQ_PGM, 1);
  return 0;
}

void __real_nxrs_sq_ready(struct sq_service *);
int __real_nxrs_cq_thread_join(void *, void **);

/* These wrappers return before/after the event loop. Its call depth, clock
 * reads and hot functions remain untouched, unlike a coordinator wrapper. */
SQ_IRAM void __wrap_nxrs_sq_ready(struct sq_service *service) {
  if (nxrs_sq_id(service) == 0 && start_counters() != 0) nxrs_sq_failed(service);
  __real_nxrs_sq_ready(service);
}

SQ_IRAM int __wrap_nxrs_cq_thread_join(void *thread, void **result) {
  if (!state.active) return __real_nxrs_cq_thread_join(thread, result);
  write_eri(SQ_PGM, 0);
  state.active = 0;
  uint32_t value0 = read_eri(SQ_PM0), value1 = read_eri(SQ_PM0 + 4u);
  uint32_t overflow = (read_eri(SQ_STAT0) | read_eri(SQ_STAT0 + 4u)) & 1u;
  for (unsigned id = 0; id < 2; id++) {
    write_eri(SQ_CTRL0 + 4u * id, state.controls[id]);
    write_eri(SQ_PM0 + 4u * id, state.counters[id]);
  }
  write_eri(SQ_PGM, state.pgm);
  int joined = __real_nxrs_cq_thread_join(thread, result);
  printf("SQ_PM mode=%s select0=%u mask0=%u value0=%u select1=5 mask1=32 value1=%u overflow=%u\n",
         state.mode, (unsigned)state.select, (unsigned)state.mask,
         (unsigned)value0, (unsigned)value1, (unsigned)overflow);
  return overflow ? 1 : joined;
}
