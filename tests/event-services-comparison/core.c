/* SPDX-License-Identifier: MIT */
#include "contract.h"
#include <limits.h>
#include <string.h>

_Static_assert(sizeof(struct es_event) == ES_EVENT_BYTES, "event ABI");
static const uint32_t offsets[3] = {1, 3, 7};
static uint32_t period(unsigned kind, unsigned profile) {
  static const uint32_t periods[3] = {250000, 50000, 100000};
  return (profile == 2 && kind == 1 ? 10000 : periods[kind]) * ES_HZ;
}
uint32_t es_destination(uint32_t source, uint32_t peer) {
  return peer < 3 ? (source + offsets[peer]) % 20 : UINT32_MAX;
}
uint32_t es_token(uint32_t source, uint32_t destination, uint32_t kind,
                  uint32_t sequence) {
  return ((source << 24) | (destination << 16) | (kind << 8)) ^
         (sequence * UINT32_C(0x9e3779b9)) ^ UINT32_C(0xa5a5a5a5);
}
uint32_t es_work_value(uint32_t value, uint32_t token, uint32_t iterations) {
  for (uint32_t iteration = 0; iteration < iterations; ++iteration) {
    uint32_t rotated = (value << 5) | (value >> 27);
    uint32_t mixed = iteration * UINT32_C(0x7f4a7c15);
    value = rotated * UINT32_C(0x9e3779b9) + (token ^ mixed);
  }
  return value;
}
void es_make_event(struct es_event *event, uint32_t source, uint32_t peer,
                   const struct es_release *release, uint32_t posted) {
  memset(event, 0, sizeof(*event));
  event->scheduled_cycles = release->scheduled_cycles;
  event->posted_cycles = posted;
  event->sequence = release->sequence;
  event->source = source;
  event->destination = es_destination(source, peer);
  event->kind = release->kind;
  event->peer = peer;
  event->token =
      es_token(source, event->destination, release->kind, release->sequence);
  for (unsigned i = 0; i < sizeof(event->payload); ++i)
    event->payload[i] = (uint8_t)(event->token >> (8 * (i % 4))) ^ (uint8_t)i;
}
void es_schedule_init(struct es_schedule *schedule, uint32_t service,
                      uint32_t profile) {
  memset(schedule, 0, sizeof(*schedule));
  schedule->service = service;
  schedule->profile = profile;
  for (unsigned kind = 0; kind < 3; ++kind)
    schedule->next[kind] =
        period(kind, profile) +
        (profile == 2 ? 0 : (service * 1000 + kind * 2000) * ES_HZ);
}
uint32_t es_schedule_next(const struct es_schedule *schedule) {
  uint32_t deadline = ES_DURATION_US * ES_HZ;
  for (unsigned kind = 0; kind < 3; ++kind)
    if (schedule->next[kind] < deadline)
      deadline = schedule->next[kind];
  return deadline;
}
int es_schedule_due(struct es_schedule *schedule, uint32_t now,
                    struct es_release *release) {
  unsigned chosen = 3;
  uint32_t deadline = ES_DURATION_US * ES_HZ;
  for (unsigned kind = 0; kind < 3; ++kind)
    if (schedule->next[kind] < deadline) {
      deadline = schedule->next[kind];
      chosen = kind;
    }
  if (chosen == 3 || now < deadline)
    return 0;
  unsigned burst = chosen == 1 ? (schedule->profile == 2   ? 16
                                  : schedule->profile == 1 ? 2
                                                           : 1)
                               : 1;
  *release =
      (struct es_release){deadline, schedule->sequence[chosen], burst, chosen};
  schedule->sequence[chosen] += burst;
  schedule->next[chosen] += period(chosen, schedule->profile);
  return 1;
}
int es_handle(struct es_state *state, const struct es_event *event,
              uint32_t service) {
  if (service >= 20 || event->source >= 20 || event->kind >= 3 ||
      event->peer >= 3 || event->destination != service ||
      es_destination(event->source, event->peer) != service ||
      event->token !=
          es_token(event->source, service, event->kind, event->sequence))
    return -1;
  struct es_flow *flow = &state->received[event->peer * 3 + event->kind];
  if (flow->initialized && event->sequence <= flow->last_sequence)
    return -1;
  for (unsigned i = 0; i < sizeof(event->payload); ++i)
    if (event->payload[i] !=
        ((uint8_t)(event->token >> (8 * (i % 4))) ^ (uint8_t)i))
      return -1;
  ++flow->count;
  flow->digest += event->token;
  flow->last_sequence = event->sequence;
  flow->initialized = 1;
  state->value = ((state->value << 3) | (state->value >> 29)) ^ event->token;
  state->value += UINT32_C(0x7f4a7c15);
  return 0;
}
static uint32_t hist_bound(unsigned index) {
  if (index == 63)
    return UINT32_MAX;
  uint32_t power = UINT32_C(1) << (index / 2);
  return power + (index % 2 ? power / 2 : 0);
}
void es_hist_add(struct es_histogram *histogram, uint32_t microseconds) {
  unsigned bin = 0;
  while (bin < 63 && microseconds > hist_bound(bin))
    ++bin;
  ++histogram->bins[bin];
  ++histogram->count;
  if (microseconds > histogram->maximum)
    histogram->maximum = microseconds;
}
uint32_t es_hist_percentile(const struct es_histogram *histogram,
                            uint32_t percent) {
  if (!histogram->count || !percent)
    return 0;
  if (percent > 100)
    percent = 100;
  uint32_t rank = (uint32_t)(((uint64_t)histogram->count * percent + 99) / 100),
           sum = 0;
  for (unsigned i = 0; i < 64; ++i) {
    sum += histogram->bins[i];
    if (sum >= rank) {
      uint32_t bound = hist_bound(i);
      return bound < histogram->maximum ? bound : histogram->maximum;
    }
  }
  return histogram->maximum;
}
