/* SPDX-License-Identifier: MIT
 * Diagnostic link wrappers only; not included in normal demo images. Each
 * received event is split at worker-entry/record boundaries. A bounded stamp
 * ring detects overwritten/missing predecessors instead of guessing timings.
 */
#include "qualification.h"
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define TRACE_SLOTS 64u
uint32_t es_platform_cycles(void);
int __real_nxrs_sq_run(int, char **);
int __real_nxrs_sq_wait(struct sq_service *, const struct sq_event *, struct sq_event *);
void __real_nxrs_sq_record(struct sq_service *, const struct sq_event *);
int __real_nxrs_sq_led_apply(struct sq_service *, unsigned);

struct stamp { atomic_uint sequence, cycles; };
struct trace {
  struct stamp stamps[TRACE_SLOTS];
  uint32_t received_at, led_elapsed;
  uint64_t transit, worker, led, end;
  unsigned events, errors;
};
static struct trace traces[SQ_MAX_SERVICES];
static unsigned trace_count;

int __wrap_nxrs_sq_wait(struct sq_service *service, const struct sq_event *pending,
                        struct sq_event *event) {
  int result = __real_nxrs_sq_wait(service, pending, event);
  uint32_t now = es_platform_cycles();
  unsigned id = nxrs_sq_id(service);
  if (result > 0) {
    struct trace *trace = &traces[id];
    uint32_t previous = event->origin_cycles;
    if (id != 0) {
      struct stamp *stamp = &traces[id - 1].stamps[event->sequence % TRACE_SLOTS];
      unsigned first = atomic_load_explicit(&stamp->sequence, memory_order_acquire);
      uint32_t saved = atomic_load_explicit(&stamp->cycles, memory_order_acquire);
      unsigned last = atomic_load_explicit(&stamp->sequence, memory_order_acquire);
      if (first != event->sequence || last != event->sequence) trace->errors++;
      else previous = saved;
    }
    trace->transit += (uint32_t)(now - previous);
    trace->received_at = now;
    trace->led_elapsed = 0;
  }
  return result;
}

int __wrap_nxrs_sq_led_apply(struct sq_service *service, unsigned level) {
  uint32_t begin = es_platform_cycles();
  int result = __real_nxrs_sq_led_apply(service, level);
  uint32_t elapsed = es_platform_cycles() - begin;
  traces[nxrs_sq_id(service)].led_elapsed = elapsed;
  return result;
}

void __wrap_nxrs_sq_record(struct sq_service *service, const struct sq_event *event) {
  uint32_t now = es_platform_cycles();
  struct trace *trace = &traces[nxrs_sq_id(service)];
  trace->worker += (uint32_t)(now - trace->received_at);
  trace->led += trace->led_elapsed;
  trace->end += (uint32_t)(now - event->origin_cycles);
  trace->events++;
  struct stamp *stamp = &trace->stamps[event->sequence % TRACE_SLOTS];
  /* Invalidate before reuse. The acquire timestamp load makes invalidation
   * visible to a reader that overlaps an overwrite of this bounded slot. */
  atomic_store_explicit(&stamp->sequence, UINT32_MAX, memory_order_release);
  atomic_store_explicit(&stamp->cycles, now, memory_order_release);
  atomic_store_explicit(&stamp->sequence, event->sequence, memory_order_release);
  __real_nxrs_sq_record(service, event);
}

int __wrap_nxrs_sq_run(int argc, char **argv) {
  /* No IRQ interpretation: this diagnostic only qualifies message sources. */
  if (argc > 4) return 2;
  memset(traces, 0, sizeof(traces));
  for (unsigned id = 0; id < SQ_MAX_SERVICES; id++)
    for (unsigned slot = 0; slot < TRACE_SLOTS; slot++) {
      atomic_init(&traces[id].stamps[slot].sequence, UINT32_MAX);
      atomic_init(&traces[id].stamps[slot].cycles, 0);
    }
  trace_count = 0;
  int result = __real_nxrs_sq_run(argc, argv);
  /* The run has joined every normal-path worker before returning. */
  if (result != 0) return result;
  for (unsigned id = 0; id < SQ_MAX_SERVICES && traces[id].events; id++) {
    struct trace *trace = &traces[id];
    trace_count++;
    printf("SQ_TRACE id=%u events=%u transit_cycles=%llu worker_cycles=%llu led_cycles=%llu end_cycles=%llu errors=%u\n",
           id, trace->events, (unsigned long long)trace->transit,
           (unsigned long long)trace->worker, (unsigned long long)trace->led,
           (unsigned long long)trace->end, trace->errors);
    if (trace->errors) result = 1;
  }
  if (trace_count < 3) result = 1;
  return result;
}
