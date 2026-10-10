/* SPDX-License-Identifier: MIT
 * Lean qualification keeps counts/maxima, not latency distributions.
 */
#ifndef NXRS_EVENT_SERVICES_INSTRUMENTATION_H
#define NXRS_EVENT_SERVICES_INSTRUMENTATION_H
#include "contract.h"
#ifndef ES_LEAN
#define ES_LEAN 0
#endif
#if ES_LEAN
typedef struct { uint32_t count, maximum; } es_distribution;
static inline void es_distribution_add(es_distribution *d, uint32_t value) {
  ++d->count;
  if (value > d->maximum) d->maximum = value;
}
static inline uint32_t es_distribution_percentile(const es_distribution *d,
                                                 uint32_t percent) {
  (void)d; (void)percent;
  return 0; /* No percentile is available; lean reports omit latency fields. */
}
#else
typedef struct es_histogram es_distribution;
#define es_distribution_add es_hist_add
#define es_distribution_percentile es_hist_percentile
#endif
static inline void es_distribution_merge(es_distribution *to,
                                         const es_distribution *from) {
#if !ES_LEAN
  for (unsigned i = 0; i < ES_HIST_BINS; ++i) to->bins[i] += from->bins[i];
#endif
  to->count += from->count;
  if (from->maximum > to->maximum) to->maximum = from->maximum;
}
#endif
