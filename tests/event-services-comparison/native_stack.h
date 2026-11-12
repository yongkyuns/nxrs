/* SPDX-License-Identifier: MIT
 * Experiment-only stack observations. No printing occurs in worker context.
 */
#ifndef NXRS_EVENT_SERVICES_NATIVE_STACK_H
#define NXRS_EVENT_SERVICES_NATIVE_STACK_H
#include "platform.h"
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

struct es_stack_sample {
  uint32_t reserved;
  uint32_t used;
  uint32_t available;
};

static inline struct es_stack_sample es_stack_unknown(size_t reserved) {
  return (struct es_stack_sample){.reserved = (uint32_t)reserved};
}

static inline struct es_stack_sample es_stack_observed(size_t reserved,
                                                       size_t used) {
  struct es_stack_sample sample = es_stack_unknown(reserved);
  if (reserved <= UINT32_MAX && used <= reserved) {
    sample.used = (uint32_t)used;
    sample.available = 1;
  }
  return sample;
}

static inline void es_stack_print(const char *scope, unsigned id,
                                  size_t requested,
                                  struct es_stack_sample sample,
                                  const char *capture) {
  printf("ES_STACK scope=%s id=%u requested=%zu reserved=%u used=%u "
         "available=%u capture=%s\n",
         scope, id, requested, sample.reserved, sample.used, sample.available,
         capture);
}
#endif
