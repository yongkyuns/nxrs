/* SPDX-License-Identifier: MIT — experiment controls, not production APIs. */
#ifndef NXRS_EVENT_CONTROLS_H
#define NXRS_EVENT_CONTROLS_H
#include "contract.h"
#ifndef ES_TIMER_MS
#define ES_TIMER_MS 10
#endif
#define ES_WORK_SHORT 10000u
#define ES_WORK_LONG 400000u
#define ES_WORK_MEDIUM 100000u
#define ES_IO_WAIT_US 3000u
static inline uint32_t es_work_iterations(unsigned profile) {
  return profile == 3 ? ES_WORK_SHORT
                      : (profile == 4 ? ES_WORK_LONG
                                      : (profile == 7 ? ES_WORK_MEDIUM : 0));
}
/* Only service zero owns the extra synchronous work/LED. Other services keep
 * the original handler so peer response measures interference, not 20 busy
 * handlers collectively exceeding the CPU budget. */
static inline int es_work_event(unsigned id, const struct es_event *event) {
  return id == 0 && event->kind == 1;
}
int es_control_apply(unsigned id, const struct es_event *event);
#endif
