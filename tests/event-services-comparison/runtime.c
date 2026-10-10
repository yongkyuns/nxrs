/* SPDX-License-Identifier: MIT */
#include "runtime.h"
#include "controls.h"
#include "hal.h"
#include "saturation.h"
#include <stdio.h>
#include <string.h>
static uint32_t epoch, profile;
static struct es_state states[20];
static struct es_diagnostics diagnostics[20];
/* Service zero is the sole writer. Read/reset only with all threads joined. */
static es_distribution work_duration;
static uint32_t work_jobs, hal_calls, hal_errors;
static struct {
  es_distribution io_duration;
  uint32_t io_jobs, yields, work_digest;
} scheduling_extra;
uint32_t es_now(void) { return es_platform_cycles() - epoch; }
uint32_t es_profile(void) { return profile; }
struct es_state *es_runtime_state(unsigned id) {
  return id < 20 ? &states[id] : NULL;
}
static uint32_t micros(uint32_t cycles) {
  return cycles / ES_HZ + (cycles % ES_HZ != 0);
}
void es_record_send(unsigned id, const struct es_event *event, int result,
                    uint32_t posted) {
  struct es_diagnostics *d = &diagnostics[id];
  ++d->attempted;
  es_distribution_add(&d->publication, micros(posted - event->scheduled_cycles));
  if (result == 1) {
    ++d->rejected;
    return;
  }
  if (result != 0) {
    ++d->errors;
    return;
  }
  struct es_flow *flow = &d->accepted[event->peer * 3 + event->kind];
  ++flow->count;
  flow->digest += event->token;
  flow->initialized = 1;
  flow->last_sequence = event->sequence;
}
void es_record_receive(unsigned id, const struct es_event *event, int result,
                       uint32_t started, uint32_t finished) {
  struct es_diagnostics *d = &diagnostics[id];
  if (result) {
    ++d->errors;
    return;
  }
  uint32_t start_us = micros(started - event->scheduled_cycles);
  uint32_t finish_us = micros(finished - event->scheduled_cycles);
  if (es_work_iterations(profile) && es_work_event(id, event)) {
    ++work_jobs;
    es_distribution_add(&work_duration, micros(finished - started));
    scheduling_extra.work_digest += states[id].value;
  }
  if (profile == 8 && es_work_event(id, event)) {
    ++scheduling_extra.io_jobs;
    es_distribution_add(&scheduling_extra.io_duration, micros(finished - started));
  }
  es_distribution_add(&d->start, start_us);
  es_distribution_add(&d->finish, finish_us);
  es_distribution_add(&d->queue_start, micros(started - event->posted_cycles));
  if (event->kind == 0)
    es_distribution_add(&d->control_start, start_us);
  static const unsigned deadlines[3] = {20000, 40000, 80000};
  if (finish_us > deadlines[event->kind])
    ++d->missed[event->kind];
  if (finished > d->last_finish)
    d->last_finish = finished;
}
void es_record_pending(unsigned id, uint32_t next) {
  diagnostics[id].pending = next < ES_DURATION_US * ES_HZ;
}
int es_control_apply(unsigned id, const struct es_event *event) {
  if (profile == 8 && es_work_event(id, event))
    return es_platform_io_wait(ES_IO_WAIT_US);
  if (profile != 5 || id != 0 || event->kind != 0)
    return 0;
  ++hal_calls;
  int result = es_hal_apply(event->token & 1u);
  if (result)
    ++hal_errors;
  return result;
}
#ifndef ES_RUST
static void service_loop(unsigned id) {
  struct es_schedule schedule;
  es_schedule_init(&schedule, id, profile);
  const uint32_t stop = (ES_DURATION_US + ES_DRAIN_US) * ES_HZ;
  while (es_now() < stop) {
    struct es_release release;
    if (es_schedule_due(&schedule, es_now(), &release))
      for (unsigned n = 0; n < release.count; ++n)
        for (unsigned peer = 0; peer < 3; ++peer) {
          struct es_event event;
          struct es_release current = release;
          current.sequence += n;
          es_make_event(&event, id, peer, &current, 0);
          uint32_t posted = es_now();
          event.posted_cycles = posted;
          int result = es_platform_send(event.destination, event.kind, &event);
          es_record_send(id, &event, result, posted);
        }
    uint32_t now = es_now(), deadline = es_schedule_next(&schedule);
    if (deadline >= ES_DURATION_US * ES_HZ)
      deadline = stop;
    uint32_t timeout = deadline > now ? deadline - now : 0;
    struct es_event event;
    int available = es_platform_wait(id, timeout, &event);
    if (available < 0) {
      ++diagnostics[id].errors;
      break;
    }
    if (available) {
      uint32_t started = es_now();
      int result = es_handle(&states[id], &event, id);
      if (!result) {
        if (es_work_event(id, &event))
          states[id].value = es_work_value(states[id].value, event.token,
                                           es_work_iterations(profile));
        result = es_control_apply(id, &event);
      }
      uint32_t finished = es_now();
      es_record_receive(id, &event, result, started, finished);
    }
  }
  es_record_pending(id, es_schedule_next(&schedule));
}
#else
static void (*rust_loop)(unsigned);
void es_register_rust_loop(void (*loop)(unsigned)) { rust_loop = loop; }
#endif
static void *service_entry(void *argument) {
  unsigned id = (unsigned)(uintptr_t)argument;
  if (es_platform_arrive()) {
    ++diagnostics[id].errors;
    return NULL;
  }
#ifdef ES_RUST
  rust_loop(id);
#else
  service_loop(id);
#endif
  return NULL;
}
int es_run_main(int argc, const char *const *argv) {
  if (argc != 2 || !argv || !argv[1])
    return 1;
  printf("ES_INSTRUMENTATION mode=%s histogram_bins=%u\n",
         ES_LEAN ? "lean" : "full", ES_LEAN ? 0u : ES_HIST_BINS);
  if (!strcmp(argv[1], "saturation"))
    return es_run_saturation();
  if (!strcmp(argv[1], "normal"))
    profile = 0;
  else if (!strcmp(argv[1], "burst"))
    profile = 1;
  else if (!strcmp(argv[1], "overload"))
    profile = 2;
  else if (!strcmp(argv[1], "work-short"))
    profile = 3;
  else if (!strcmp(argv[1], "work-long"))
    profile = 4;
  else if (!strcmp(argv[1], "hal"))
    profile = 5;
  else if (!strcmp(argv[1], "work-medium"))
    profile = 7;
  else if (!strcmp(argv[1], "io-wait"))
    profile = 8;
  else
    return 1;
  memset(states, 0, sizeof(states));
  memset(diagnostics, 0, sizeof(diagnostics));
  memset(&work_duration, 0, sizeof(work_duration));
  memset(&scheduling_extra, 0, sizeof(scheduling_extra));
  work_jobs = hal_calls = hal_errors = 0;
  uint32_t before = es_platform_heap_used();
  if (es_platform_init()) {
    if (profile == 5)
      es_hal_close();
    puts("ES_FAIL stage=queues");
    return 1;
  }
  if (profile == 5 && es_hal_init()) {
    es_hal_close();
    es_platform_close();
    puts("ES_FAIL stage=hal-init");
    return 1;
  }
  int spawn_error = 0;
  for (unsigned id = 0; id < 20; ++id)
    if (es_platform_spawn(service_entry)) {
      spawn_error = 1;
      break;
    }
  if (spawn_error || es_platform_release(&epoch)) {
    puts("ES_FAIL stage=start");
    es_platform_close();
    if (profile == 5)
      es_hal_close();
    return 1;
  }
  uint32_t live = es_platform_heap_used();
  if (es_platform_join()) {
    if (profile == 5)
      es_hal_close();
    puts("ES_FAIL stage=join");
    return 1;
  }
  uint32_t attempted = 0, accepted = 0, received = 0, rejected = 0, errors = 0,
           missed = 0;
  uint32_t finish = 0, worst_service_p99 = 0;
  es_distribution publication = {0}, start = {0}, done = {0}, control = {0};
  es_distribution queue_start = {0};
  for (unsigned id = 0; id < 20; ++id) {
    struct es_diagnostics *d = &diagnostics[id];
    attempted += d->attempted;
    rejected += d->rejected;
    errors += d->errors + d->pending;
    missed += d->missed[0] + d->missed[1] + d->missed[2];
    if (d->last_finish > finish)
      finish = d->last_finish;
    es_distribution_merge(&publication, &d->publication);
    es_distribution_merge(&start, &d->start);
    es_distribution_merge(&done, &d->finish);
    es_distribution_merge(&control, &d->control_start);
    es_distribution_merge(&queue_start, &d->queue_start);
    uint32_t p99 = es_distribution_percentile(&d->start, 99);
    if (p99 > worst_service_p99)
      worst_service_p99 = p99;
    for (unsigned peer = 0; peer < 3; ++peer)
      for (unsigned kind = 0; kind < 3; ++kind) {
        unsigned index = peer * 3 + kind,
                 destination = es_destination(id, peer);
        struct es_flow *sent = &d->accepted[index],
                       *got = &states[destination].received[index];
        accepted += sent->count;
        received += states[id].received[index].count;
        if (sent->count != got->count || sent->digest != got->digest ||
            sent->initialized != got->initialized ||
            (sent->initialized && sent->last_sequence != got->last_sequence))
          ++errors;
      }
    printf("ES_SERVICE id=%u received=%u start_p99_us=%u start_max_us=%u "
           "finish_p99_us=%u "
           "control_p99_us=%u missed_control=%u missed_data=%u "
           "missed_status=%u rejected=%u "
           "queue_p99_us=%u queue_max_us=%u\n",
           id, d->start.count, p99, d->start.maximum,
           es_distribution_percentile(&d->finish, 99),
           es_distribution_percentile(&d->control_start, 99), d->missed[0],
           d->missed[1], d->missed[2], d->rejected,
           es_distribution_percentile(&d->queue_start, 99), d->queue_start.maximum);
  }
  if (attempted != accepted + rejected || accepted != received)
    ++errors;
  es_platform_resources();
  printf("ES_MEMORY application_state=%u diagnostics=%u heap_before=%u "
         "heap_live=%u\n",
         (unsigned)sizeof(states), (unsigned)sizeof(diagnostics), before, live);
  unsigned peak = es_platform_queue_peak();
  es_platform_close();
  if (profile == 5 && es_hal_close()) {
    ++errors;
    ++hal_errors;
  }
  printf(
      "ES_CONTROL timer_ms=%u work_iterations=%u work_jobs=%u work_p99_us=%u "
      "work_max_us=%u hal_calls=%u hal_errors=%u diagnostic_bytes=%u\n",
      ES_TIMER_MS, es_work_iterations(profile), work_jobs,
      es_distribution_percentile(&work_duration, 99), work_duration.maximum, hal_calls,
      hal_errors,
      (unsigned)(sizeof(work_duration) + sizeof(work_jobs) + sizeof(hal_calls) +
                 sizeof(hal_errors)));
  printf("ES_SCHED policy=native work_mode=monolithic budget_events=4 "
         "budget_us=500 chunk_iterations=10000 io_wait_us=%u yields=0 "
         "io_jobs=%u io_p99_us=%u io_max_us=%u work_digest=%u diagnostic_bytes=%u\n",
         ES_IO_WAIT_US, scheduling_extra.io_jobs,
         es_distribution_percentile(&scheduling_extra.io_duration, 99),
         scheduling_extra.io_duration.maximum, scheduling_extra.work_digest,
         (unsigned)sizeof(scheduling_extra));
  printf(
      "ES_RESULT platform=%s profile=%s services=20 queues=%u event_bytes=64 "
      "slots=480 "
      "attempted=%u accepted=%u received=%u rejected=%u errors=%u missed=%u "
      "publication_p99_us=%u publication_max_us=%u start_p99_us=%u "
      "start_max_us=%u "
      "finish_p99_us=%u finish_max_us=%u control_p99_us=%u control_max_us=%u "
      "worst_service_p99_us=%u queue_peak_observed=%u last_finish_us=%u "
      "delivery_ok=%u capacity_ok=%u deadlines_ok=%u queue_p99_us=%u "
      "queue_max_us=%u\n",
      es_platform_name(), argv[1], ES_SERVICES * ES_QUEUES_PER_SERVICE,
      attempted, accepted, received, rejected, errors, missed,
      es_distribution_percentile(&publication, 99), publication.maximum,
      es_distribution_percentile(&start, 99), start.maximum,
      es_distribution_percentile(&done, 99), done.maximum,
      es_distribution_percentile(&control, 99), control.maximum, worst_service_p99,
      peak, micros(finish), !errors, !rejected, !missed,
      es_distribution_percentile(&queue_start, 99), queue_start.maximum);
  puts(errors ? "ES_FAIL stage=protocol" : "ES_PASS");
  return errors ? 1 : 0;
}
