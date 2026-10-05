/* SPDX-License-Identifier: MIT */
#include "saturation.h"
#include "platform.h"

#include <stdio.h>
#include <string.h>

#define ES_SAT_CYCLES 3u
#define ES_SAT_STACK_BYTES (ES_SERVICES * 4096u)

_Static_assert(sizeof(struct es_event) == ES_EVENT_BYTES,
               "event layout must occupy exactly 64 bytes");
_Static_assert(ES_SERVICES * ES_QUEUES_PER_SERVICE * ES_QUEUE_SLOTS == 480,
               "both queue layouts must provide exactly 480 slots");
_Static_assert(ES_SAT_STACK_BYTES == 81920,
               "probe stacks must total exactly 81,920 bytes");

static void *es_saturation_probe(void *argument) {
  (void)argument;
  (void)es_platform_arrive();
  return NULL;
}

static unsigned es_saturation_queue_count(void) {
  return ES_SERVICES * ES_QUEUES_PER_SERVICE;
}

static unsigned es_saturation_kind(unsigned queue, unsigned slot) {
  return ES_MAILBOX ? slot % ES_KINDS : queue % ES_QUEUES_PER_SERVICE;
}

static void es_saturation_make_event(struct es_event *event,
                                     unsigned destination, unsigned kind,
                                     uint32_t sequence) {
  unsigned source = (destination + ES_SERVICES - 1u) % ES_SERVICES;
  struct es_release release = {
      .scheduled_cycles = 0, .sequence = sequence, .count = 1, .kind = kind};
  es_make_event(event, source, 0, &release, 0);
}

static int es_saturation_event_valid(const struct es_event *event,
                                     unsigned destination,
                                     unsigned local_kind_count[ES_KINDS],
                                     unsigned local_slot) {
  unsigned kind;
  uint32_t sequence;
  if (ES_MAILBOX) {
    kind = local_slot % ES_KINDS;
    sequence =
        destination * (ES_QUEUE_SLOTS / ES_KINDS) + local_slot / ES_KINDS;
  } else {
    kind = event->kind;
    if (kind >= ES_KINDS) {
      return 0;
    }
    sequence = destination * ES_QUEUE_SLOTS + local_kind_count[kind];
  }
  if (event->kind != kind || event->sequence != sequence ||
      event->destination != destination ||
      event->source != (destination + ES_SERVICES - 1u) % ES_SERVICES ||
      event->peer != 0) {
    return 0;
  }
  struct es_event expected;
  es_saturation_make_event(&expected, destination, kind, sequence);
  return memcmp(event, &expected, sizeof(expected)) == 0;
}

static int es_saturation_depths(unsigned expected, unsigned *equal) {
  unsigned matches = 0;
  for (unsigned destination = 0; destination < ES_SERVICES; destination++) {
    for (unsigned queue = 0; queue < ES_QUEUES_PER_SERVICE; queue++) {
      unsigned kind = ES_MAILBOX ? 0u : queue;
      int depth = es_platform_queue_depth(destination, kind);
      if (depth < 0) {
        *equal = matches;
        return -1;
      }
      if ((unsigned)depth == expected) {
        matches++;
      }
    }
  }
  *equal = matches;
  return matches == es_saturation_queue_count() ? 0 : -1;
}

static int es_saturation_cycle(unsigned cycle, unsigned *errors) {
  uint32_t empty_heap = es_platform_heap_used();
  unsigned filled = 0, drained = 0, overflow_rejected = 0, depth_full = 0;
  uint32_t sequence[ES_KINDS] = {0};

  for (unsigned destination = 0; destination < ES_SERVICES; destination++) {
    for (unsigned queue = 0; queue < ES_QUEUES_PER_SERVICE; queue++) {
      for (unsigned slot = 0; slot < ES_QUEUE_SLOTS; slot++) {
        unsigned kind = es_saturation_kind(queue, slot);
        struct es_event event;
        es_saturation_make_event(&event, destination, kind, sequence[kind]++);
        int sent = es_platform_send(destination, kind, &event);
        if (sent != 0) {
          ++*errors;
        } else {
          ++filled;
        }
      }
    }
  }

  if (filled != ES_SERVICES * ES_QUEUES_PER_SERVICE * ES_QUEUE_SLOTS) {
    ++*errors;
  }
  if (es_saturation_depths(ES_QUEUE_SLOTS, &depth_full) != 0) {
    ++*errors;
  }

  for (unsigned destination = 0; destination < ES_SERVICES; destination++) {
    for (unsigned queue = 0; queue < ES_QUEUES_PER_SERVICE; queue++) {
      unsigned kind = ES_MAILBOX ? 0u : queue;
      int before = es_platform_queue_depth(destination, kind);
      struct es_event overflow;
      es_saturation_make_event(&overflow, destination, kind, UINT32_MAX);
      int sent = es_platform_send(destination, kind, &overflow);
      int after = es_platform_queue_depth(destination, kind);
      if (sent == 1) {
        ++overflow_rejected;
      } else {
        ++*errors;
      }
      if (before != (int)ES_QUEUE_SLOTS || after != before) {
        ++*errors;
      }
    }
  }

  uint32_t full_heap = es_platform_heap_used();
  es_platform_resources();

  for (unsigned destination = 0; destination < ES_SERVICES; destination++) {
    unsigned per_kind[ES_KINDS] = {0};
    unsigned local_slot = 0;
    const unsigned expected_count = ES_QUEUES_PER_SERVICE * ES_QUEUE_SLOTS;
    for (unsigned index = 0; index < expected_count; index++) {
      struct es_event event;
      int received = es_platform_wait(destination, 0, &event);
      if (received != 1) {
        ++*errors;
        continue;
      }
      ++drained;
      if (ES_MAILBOX) {
        if (!es_saturation_event_valid(&event, destination, per_kind,
                                       local_slot)) {
          ++*errors;
        }
        if (event.kind < ES_KINDS) {
          ++per_kind[event.kind];
        }
        ++local_slot;
      } else {
        if (event.kind >= ES_KINDS ||
            !es_saturation_event_valid(&event, destination, per_kind,
                                       local_slot)) {
          ++*errors;
        }
        if (event.kind < ES_KINDS) {
          ++per_kind[event.kind];
        }
        ++local_slot;
      }
    }
  }

  if (drained != ES_SERVICES * ES_QUEUES_PER_SERVICE * ES_QUEUE_SLOTS) {
    ++*errors;
  }
  unsigned depth_zero = 0;
  if (es_saturation_depths(0, &depth_zero) != 0) {
    ++*errors;
  }
  uint32_t drained_heap = es_platform_heap_used();
  printf("ES_SAT_CYCLE cycle=%u empty_heap=%u full_heap=%u drained_heap=%u "
         "filled=%u drained=%u overflow_rejected=%u depth_full=%u "
         "depth_zero=%u errors=%u\n",
         cycle, empty_heap, full_heap, drained_heap, filled, drained,
         overflow_rejected, depth_full, depth_zero, *errors);
  return 0;
}

int es_run_saturation(void) {
  unsigned errors = 0, spawned = 0;
  int initialized = 0;
  if (es_platform_init() != 0) {
    puts("ES_FAIL stage=saturation_init");
    return 1;
  }
  initialized = 1;
  for (; spawned < ES_SERVICES; spawned++) {
    if (es_platform_spawn(es_saturation_probe) != 0) {
      ++errors;
      break;
    }
  }
  if (spawned == ES_SERVICES && es_platform_wait_ready() != 0) {
    ++errors;
  }
  if (spawned != ES_SERVICES) {
    ++errors;
  }

  if (!errors) {
    for (unsigned cycle = 0; cycle < ES_SAT_CYCLES; cycle++) {
      (void)es_saturation_cycle(cycle, &errors);
    }
  }

  uint32_t epoch = 0;
  if (es_platform_release(&epoch) != 0) {
    ++errors;
  }
  (void)epoch;
  if (es_platform_join() != 0) {
    ++errors;
  }
  if (initialized) {
    es_platform_close();
  }

  printf("ES_SAT_RESULT platform=%s queues=%u slots=%u event_bytes=%u "
         "cycles=%u errors=%u stacks=%u\n",
         es_platform_name(), es_saturation_queue_count(),
         ES_SERVICES * ES_QUEUES_PER_SERVICE * ES_QUEUE_SLOTS, ES_EVENT_BYTES,
         ES_SAT_CYCLES, errors, ES_SAT_STACK_BYTES);
  puts(errors ? "ES_FAIL stage=saturation" : "ES_PASS");
  return errors ? 1 : 0;
}
