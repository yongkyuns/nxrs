/* SPDX-License-Identifier: MIT */
#ifndef NXRS_EVENT_SERVICES_RUNTIME_H
#define NXRS_EVENT_SERVICES_RUNTIME_H
#include "platform.h"
#include "instrumentation.h"
struct es_diagnostics {
  struct es_flow accepted[9];
  es_distribution publication, start, finish, control_start, queue_start;
  uint32_t attempted, rejected, errors, missed[3], last_finish, pending;
};
uint32_t es_now(void);
uint32_t es_profile(void);
struct es_state *es_runtime_state(unsigned service);
void es_record_send(unsigned service, const struct es_event *, int result,
                    uint32_t posted);
void es_record_receive(unsigned service, const struct es_event *, int result,
                       uint32_t started, uint32_t finished);
void es_record_pending(unsigned service, uint32_t next);
int es_run_main(int argc, const char *const *argv);
#endif
