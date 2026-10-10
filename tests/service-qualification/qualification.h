/* SPDX-License-Identifier: MIT
 * Qualification-only scalar ABI; native POSIX layouts stay in runtime.c. */
#ifndef NXRS_SERVICE_QUALIFICATION_H
#define NXRS_SERVICE_QUALIFICATION_H
#include <stddef.h>
#include <stdint.h>

enum { SQ_STOP = 0, SQ_CONTROL = 1, SQ_DATA = 2, SQ_CLASSES = 3,
       SQ_CAPACITY = 8, SQ_STOP_CAPACITY = 1, SQ_SLOTS = 17,
       SQ_MAX_SERVICES = 20, SQ_STACK = 4096 };
struct sq_event {
  uint32_t origin_cycles;
  uint32_t sequence;
  uint32_t value;
  uint16_t source;
  uint8_t kind;
  uint8_t reserved;
};
_Static_assert(sizeof(struct sq_event) == 16, "event ABI changed");

/* These handles are borrowed for the callback lifetime, never retained. */
struct sq_service;
int nxrs_sq_run(int argc, char **argv);
void nxrs_sq_worker(struct sq_service *service);
unsigned nxrs_sq_id(const struct sq_service *service);
unsigned nxrs_sq_count(const struct sq_service *service);
void nxrs_sq_ready(struct sq_service *service);
int nxrs_sq_wait(struct sq_service *service, const struct sq_event *pending,
            struct sq_event *event);
int nxrs_sq_led_open(struct sq_service *service);
int nxrs_sq_led_apply(struct sq_service *service, unsigned level);
int nxrs_sq_led_close(struct sq_service *service);
void nxrs_sq_record(struct sq_service *service, const struct sq_event *event);
void nxrs_sq_failed(struct sq_service *service);
#endif
