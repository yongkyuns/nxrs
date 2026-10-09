/* SPDX-License-Identifier: MIT
 * Diagnostic-only NuttX adapter. Portable service/workload code is unchanged.
 * No allocation or output occurs in scheduler/IRQ hooks; rows print after join.
 */
#include <nuttx/config.h>
#include <nuttx/irq.h>
#include <nuttx/note/note_driver.h>
#include <nuttx/sched.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>
#include "runtime.h"

#if !defined(CONFIG_SCHED_INSTRUMENTATION_SWITCH) || \
    !defined(CONFIG_SCHED_INSTRUMENTATION_IRQHANDLER) || defined(CONFIG_SMP)
#  error "Handler probe requires single-core scheduler and IRQ instrumentation"
#endif

#define HP_IRAM __attribute__((section(".iram1")))
#define HP_ROWS 117
struct hp_snapshot {
  uint32_t running, other, irq, switches, clock, value;
};
struct hp_row {
  uint32_t sequence, peer, observed, running, other, irq, switches;
};
static struct hp_snapshot totals, previous, current;
static struct hp_row rows[HP_ROWS];
static pid_t target_pid = -1, running_pid = -1;
static unsigned irq_depth, row_count, errors;
static int enabled, registered;

static inline __attribute__((always_inline)) uint32_t hp_cycles(void) {
  uint32_t value;
  __asm__ volatile("rsr.ccount %0" : "=a"(value) : : "memory");
  return value;
}

static HP_IRAM void account(uint32_t now) {
  uint32_t delta = now - totals.clock;
  if (enabled) {
    if (irq_depth) totals.irq += delta;
    else if (running_pid == target_pid) totals.running += delta;
    else totals.other += delta;
  }
  totals.clock = now;
}

static HP_IRAM void hp_suspend(struct note_driver_s *driver, struct tcb_s *tcb) {
  (void)driver;
  account(hp_cycles());
  if (enabled && tcb->pid == target_pid) ++totals.switches;
  running_pid = -1;
}
static HP_IRAM void hp_resume(struct note_driver_s *driver, struct tcb_s *tcb) {
  (void)driver;
  account(hp_cycles());
  running_pid = tcb->pid;
}
static HP_IRAM void hp_irq(struct note_driver_s *driver, int irq,
                           void *handler, bool enter) {
  (void)driver; (void)irq; (void)handler;
  account(hp_cycles());
  if (enter) ++irq_depth;
  else if (irq_depth) --irq_depth;
  else if (enabled) ++errors;
}
static const struct note_driver_ops_s hp_ops = {
  .suspend = hp_suspend, .resume = hp_resume, .irqhandler = hp_irq
};
static struct note_driver_s hp_driver = {.ops = &hp_ops};

void es_probe_reset(void) {
  irqstate_t flags = enter_critical_section();
  enabled = 0;
  target_pid = -1;
  running_pid = gettid();
  irq_depth = row_count = errors = 0;
  totals = previous = current = (struct hp_snapshot){0};
  totals.clock = hp_cycles();
  if (!registered) {
    if (note_driver_register(&hp_driver)) ++errors;
    else registered = 1;
  }
  leave_critical_section(flags);
}

void es_probe_register(unsigned id) {
  if (id) return;
  irqstate_t flags = enter_critical_section();
  account(hp_cycles());
  target_pid = running_pid = gettid();
  enabled = 1;
  leave_critical_section(flags);
}

extern uint32_t es_diag_original_now(void);
uint32_t es_now(void) {
  irqstate_t flags = enter_critical_section();
  account(hp_cycles());
  uint32_t value = es_diag_original_now();
  if (enabled && gettid() == target_pid) {
    previous = current;
    current = totals;
    current.value = value;
  }
  leave_critical_section(flags);
  return value;
}

extern void es_diag_original_receive(unsigned, const struct es_event *, int,
                                     uint32_t, uint32_t);
void es_record_receive(unsigned id, const struct es_event *event, int result,
                       uint32_t started, uint32_t finished) {
  if (id == 0 && event->kind == 1) {
    if (result || row_count >= HP_ROWS || previous.value != started ||
        current.value != finished) ++errors;
    else {
      rows[row_count++] = (struct hp_row){
        event->sequence, event->peer, finished - started,
        current.running - previous.running, current.other - previous.other,
        current.irq - previous.irq, current.switches - previous.switches
      };
    }
  }
  es_diag_original_receive(id, event, result, started, finished);
}

extern void es_diag_original_resources(void);
void es_platform_resources(void) {
  irqstate_t flags = enter_critical_section();
  enabled = 0;
  leave_critical_section(flags);
  es_diag_original_resources();
  for (unsigned i = 0; i < row_count; ++i) {
    const struct hp_row *r = &rows[i];
    printf("HP_ROW sequence=%lu peer=%lu observed_cycles=%lu wall_cycles=%lu "
           "running_cycles=%lu other_cycles=%lu irq_cycles=%lu switches=%lu\n",
           (unsigned long)r->sequence, (unsigned long)r->peer,
           (unsigned long)r->observed,
           (unsigned long)(r->running + r->other + r->irq),
           (unsigned long)r->running, (unsigned long)r->other,
           (unsigned long)r->irq, (unsigned long)r->switches);
  }
  printf("HP_DONE jobs=%u errors=%u\n", row_count, errors);
}
