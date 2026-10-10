/* SPDX-License-Identifier: MIT
 * Experiment-owned wire/state contract. No production or upstream API.
 */
#ifndef NXRS_EVENT_SERVICES_CONTRACT_H
#define NXRS_EVENT_SERVICES_CONTRACT_H
#include <stdint.h>
#if defined(ES_SPEED) && defined(__GNUC__) && !defined(__clang__)
/* Application-only control. NuttX/Zephyr kernels remain size optimized. */
#pragma GCC optimize("O2")
#endif
#define ES_SERVICES 20
#define ES_KINDS 3
#define ES_PEERS 3
#define ES_FLOWS 9
#define ES_CAPACITY 8
#define ES_EVENT_BYTES 64
#define ES_HIST_BINS 64
#define ES_HZ 240
#define ES_DURATION_US 2000000u
#define ES_DRAIN_US 500000u
/* Profiles: normal=0, burst=1, overload=2. Kind: control=0,data=1,status=2. */
struct es_event {
  uint32_t scheduled_cycles, posted_cycles, sequence;
  uint8_t source, destination, kind, peer;
  uint32_t token;
  uint8_t payload[44];
};
struct es_release {
  uint32_t scheduled_cycles, sequence, count, kind;
};
struct es_schedule {
  uint32_t next[3], sequence[3], service, profile;
};
struct es_flow {
  uint32_t count, digest, last_sequence, initialized;
};
struct es_state {
  struct es_flow received[9];
  uint32_t value, errors;
};
struct es_histogram {
  uint32_t bins[64], count, maximum;
};
uint32_t es_destination(uint32_t source, uint32_t peer);
uint32_t es_token(uint32_t source, uint32_t destination, uint32_t kind,
                  uint32_t sequence);
uint32_t es_work_value(uint32_t value, uint32_t token, uint32_t iterations);
void es_make_event(struct es_event *, uint32_t source, uint32_t peer,
                   const struct es_release *, uint32_t posted);
void es_schedule_init(struct es_schedule *, uint32_t service, uint32_t profile);
int es_schedule_due(struct es_schedule *, uint32_t now, struct es_release *);
uint32_t es_schedule_next(const struct es_schedule *);
int es_handle(struct es_state *, const struct es_event *, uint32_t service);
void es_hist_add(struct es_histogram *, uint32_t microseconds);
uint32_t es_hist_percentile(const struct es_histogram *, uint32_t percent);
#endif
