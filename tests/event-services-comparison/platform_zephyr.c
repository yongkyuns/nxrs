/* SPDX-License-Identifier: MIT */
#include "clock.h"
#include "platform.h"

#include <errno.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/sys_heap.h>

#define ES_STACK_BYTES 4096u

_Static_assert(sizeof(struct es_event) == ES_EVENT_BYTES,
               "event layout must occupy exactly 64 bytes");
K_THREAD_STACK_ARRAY_DEFINE(worker_stacks, ES_SERVICES, ES_STACK_BYTES);

struct es_role {
  void *(*entry)(void *);
  void *argument;
};

static struct k_msgq queues[ES_SERVICES][ES_QUEUES_PER_SERVICE];
static char queue_buffers[ES_SERVICES][ES_QUEUES_PER_SERVICE]
                         [ES_QUEUE_SLOTS * ES_EVENT_BYTES];
_Static_assert(sizeof(queue_buffers) == 30720,
               "all queue storage must total 30,720 bytes");
static struct k_poll_event poll_events[ES_SERVICES][ES_QUEUES_PER_SERVICE];
static unsigned ready_cursor[ES_SERVICES];
static struct k_thread threads[ES_SERVICES];
static struct es_role roles[ES_SERVICES];
static int thread_joined[ES_SERVICES];
K_SEM_DEFINE(ready_gate, 0, ES_SERVICES);
K_SEM_DEFINE(go_gate, 0, ES_SERVICES);
static unsigned spawned;
static unsigned ready_consumed;
static int initialized;
static int released;
static int spawn_failed;
static _Atomic unsigned queue_peak;

static int es_gate_release(uint32_t *epoch) {
  if (released) {
    return 0;
  }
  for (unsigned i = ready_consumed; i < spawned; i++) {
    (void)k_sem_take(&ready_gate, K_FOREVER);
  }
  ready_consumed = spawned;
  if (epoch) {
    *epoch = es_platform_cycles();
  }
  released = 1;
  for (unsigned i = 0; i < spawned; i++) {
    k_sem_give(&go_gate);
  }
  return 0;
}

static void es_thread_trampoline(void *argument, void *unused1, void *unused2) {
  (void)unused1;
  (void)unused2;
  unsigned slot = (unsigned)(uintptr_t)argument;
  (void)roles[slot].entry(roles[slot].argument);
}

static unsigned es_qindex(unsigned kind) { return ES_MAILBOX ? 0u : kind; }

int es_platform_init(void) {
#ifdef __XTENSA__
  es_clock_prepare();
#endif
  if (initialized) {
    return -1;
  }
  memset(ready_cursor, 0, sizeof(ready_cursor));
  memset(roles, 0, sizeof(roles));
  memset(thread_joined, 0, sizeof(thread_joined));
  spawned = 0;
  ready_consumed = 0;
  released = 0;
  spawn_failed = 0;
  atomic_store(&queue_peak, 0);
  k_sem_reset(&ready_gate);
  k_sem_reset(&go_gate);
  for (unsigned s = 0; s < ES_SERVICES; s++) {
    for (unsigned q = 0; q < ES_QUEUES_PER_SERVICE; q++) {
      k_msgq_init(&queues[s][q], queue_buffers[s][q], ES_EVENT_BYTES,
                  ES_QUEUE_SLOTS);
      k_poll_event_init(&poll_events[s][q], K_POLL_TYPE_MSGQ_DATA_AVAILABLE,
                        K_POLL_MODE_NOTIFY_ONLY, &queues[s][q]);
    }
  }
  initialized = 1;
  return 0;
}

void es_platform_close(void) {
  if (!initialized) {
    return;
  }
  (void)es_platform_join();
  if (spawned != 0) {
    return;
  }
  initialized = 0;
}

int es_platform_send(unsigned destination, unsigned kind,
                     const struct es_event *event) {
  if (!initialized || !event || destination >= ES_SERVICES ||
      kind >= ES_KINDS) {
    return -1;
  }
  unsigned q = es_qindex(kind);
  int rc = k_msgq_put(&queues[destination][q], event, K_NO_WAIT);
  if (rc == 0) {
    unsigned seen = k_msgq_num_used_get(&queues[destination][q]);
    unsigned peak = atomic_load(&queue_peak);
    while (seen > peak &&
           !atomic_compare_exchange_weak(&queue_peak, &peak, seen)) {
    }
    return 0;
  }
  return rc == -ENOMSG || rc == -EAGAIN ? 1 : -1;
}

int es_platform_wait(unsigned service, uint32_t timeout_cycles,
                     struct es_event *event) {
  if (!initialized || !event || service >= ES_SERVICES) {
    return -1;
  }
  unsigned count = ES_QUEUES_PER_SERVICE;
  uint32_t timeout_ms =
      (uint32_t)(((uint64_t)timeout_cycles + 239999u) / 240000u);
  for (unsigned q = 0; q < count; q++) {
    poll_events[service][q].state = K_POLL_STATE_NOT_READY;
  }
  int rc = k_poll(poll_events[service], count, K_MSEC(timeout_ms));
  if (rc != 0) {
    return rc == -EAGAIN || rc == -EINTR ? 0 : -1;
  }
  unsigned cursor = ready_cursor[service];
  for (unsigned offset = 0; offset < count; offset++) {
    unsigned q = (cursor + offset) % count;
    if (poll_events[service][q].state == K_POLL_STATE_MSGQ_DATA_AVAILABLE) {
      ready_cursor[service] = (q + 1u) % count;
      poll_events[service][q].state = K_POLL_STATE_NOT_READY;
      rc = k_msgq_get(&queues[service][q], event, K_NO_WAIT);
      return rc == 0 ? 1 : (rc == -EAGAIN || rc == -ENOMSG ? 0 : -1);
    }
  }
  return 0;
}

uint32_t es_platform_cycles(void) {
#if defined(CONFIG_ARCH_POSIX)
  return (uint32_t)k_uptime_get_32() * 240000u;
#else
  uint32_t cycles;
#if defined(__XTENSA__)
  cycles = es_clock_cycles();
#else
  cycles = (uint32_t)k_cycle_get_32();
#endif
  return cycles;
#endif
}

int es_platform_spawn(void *(*entry)(void *)) {
  if (!initialized || !entry || spawned >= ES_SERVICES || released) {
    return -1;
  }
  unsigned slot = spawned;
  roles[slot].entry = entry;
  roles[slot].argument = (void *)(uintptr_t)slot;
  k_tid_t tid = k_thread_create(&threads[slot], worker_stacks[slot],
                                K_THREAD_STACK_SIZEOF(worker_stacks[slot]),
                                es_thread_trampoline, (void *)(uintptr_t)slot,
                                NULL, NULL, 5, 0, K_NO_WAIT);
  if (tid == NULL) {
    spawn_failed = 1;
    return -1;
  }
  spawned++;
  return 0;
}

int es_platform_arrive(void) {
  if (!initialized || released) {
    return -1;
  }
  k_sem_give(&ready_gate);
  return k_sem_take(&go_gate, K_FOREVER) == 0 ? 0 : -1;
}

int es_platform_wait_ready(void) {
  if (!initialized || released) {
    return -1;
  }
  for (unsigned i = ready_consumed; i < spawned; i++) {
    if (k_sem_take(&ready_gate, K_SECONDS(15)) != 0) {
      return -1;
    }
    ready_consumed++;
  }
  return 0;
}

int es_platform_release(uint32_t *epoch) {
  if (!initialized || !epoch) {
    return -1;
  }
  return es_gate_release(epoch);
}

int es_platform_io_wait(uint32_t microseconds) {
  uint32_t start = es_platform_cycles();
  uint32_t duration = microseconds * ES_HZ;
  uint32_t elapsed;
  while ((elapsed = es_platform_cycles() - start) < duration) {
    uint32_t remaining = (duration - elapsed + ES_HZ - 1) / ES_HZ;
    k_sleep(K_USEC(remaining));
  }
  return 0;
}

int es_platform_join(void) {
  if (!initialized) {
    return -1;
  }
  int rc = es_gate_release(NULL);
  int64_t deadline = k_uptime_get() + 15000;
  int all_joined = 1;
  for (unsigned i = 0; i < spawned; i++) {
    if (thread_joined[i]) {
      continue;
    }
    int64_t remaining = deadline - k_uptime_get();
    if (remaining < 0) {
      remaining = 0;
    }
    if (k_thread_join(&threads[i], K_MSEC(remaining)) != 0) {
      all_joined = 0;
      rc = -1;
    } else {
      thread_joined[i] = 1;
    }
  }
  if (all_joined) {
    spawned = 0;
  }
  return rc == 0 && !spawn_failed ? 0 : -1;
}

uint32_t es_platform_heap_used(void) {
#if defined(CONFIG_HEAP_MEM_POOL_SIZE) && CONFIG_HEAP_MEM_POOL_SIZE > 0
  extern struct k_heap _system_heap;
  struct sys_memory_stats stats;
  k_spinlock_key_t key = k_spin_lock(&_system_heap.lock);
  int rc = sys_heap_runtime_stats_get(&_system_heap.heap, &stats);
  k_spin_unlock(&_system_heap.lock, key);
  return rc == 0 ? (uint32_t)stats.allocated_bytes : 0;
#else
  return 0;
#endif
}

int es_platform_queue_depth(unsigned destination, unsigned kind) {
  if (!initialized || destination >= ES_SERVICES || kind >= ES_KINDS) {
    return -1;
  }
  return (int)k_msgq_num_used_get(&queues[destination][es_qindex(kind)]);
}

unsigned es_platform_queue_peak(void) { return atomic_load(&queue_peak); }

void es_platform_resources(void) {
  size_t queue_objects =
      sizeof(queues) + sizeof(poll_events) + sizeof(ready_cursor);
  size_t thread_objects = sizeof(threads) + sizeof(thread_joined);
  size_t entry_storage = sizeof(roles);
  size_t stack_storage =
      (size_t)ES_SERVICES * K_THREAD_STACK_SIZEOF(worker_stacks[0]);
  printf("ES_RESOURCES queue_buffers=30720 queue_objects=%zu "
         "thread_objects=%zu entry_storage=%zu stack_storage=%zu "
         "heap_allocated=%u diagnostics_queue_peaks=%zu "
         "heap_note=static_arena_counted_in_ELF "
         "note=queue_peak_observation_may_underestimate\n",
         queue_objects, thread_objects, entry_storage, stack_storage,
         (unsigned)es_platform_heap_used(), sizeof(queue_peak));
}

const char *es_platform_name(void) { return "zephyr-c"; }
