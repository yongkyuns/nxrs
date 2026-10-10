/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "clock.h"
#include "platform.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mqueue.h>
#include <poll.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifdef __NuttX__
#include <malloc.h>
#endif

#define ES_STACK_BYTES 4096u
#define ES_HOST_STACK_BYTES 65536u

_Static_assert(sizeof(struct es_event) == ES_EVENT_BYTES,
               "event layout must occupy exactly 64 bytes");

struct es_role {
  void *(*entry)(void *);
  void *argument;
};

static mqd_t queue_handles[ES_SERVICES][ES_QUEUES_PER_SERVICE];
static char queue_names[ES_SERVICES][ES_QUEUES_PER_SERVICE][48];
static int queue_opened[ES_SERVICES][ES_QUEUES_PER_SERVICE];
static struct pollfd poll_sets[ES_SERVICES][ES_QUEUES_PER_SERVICE];
static unsigned ready_cursor[ES_SERVICES];
static pthread_t threads[ES_SERVICES];
static struct es_role roles[ES_SERVICES];
static int thread_joined[ES_SERVICES];
static unsigned spawned;
static sem_t ready_gate;
static sem_t go_gate;
static int gates_ready;
static unsigned ready_consumed;
static int initialized;
static int released;
static int spawn_failed;
static _Atomic unsigned queue_peak;
static _Atomic unsigned invocation_id;

static unsigned es_qindex(unsigned kind) { return ES_MAILBOX ? 0u : kind; }

int es_platform_io_wait(uint32_t microseconds) {
  uint32_t start = es_platform_cycles();
  uint32_t duration = microseconds * ES_HZ;
  uint32_t elapsed;
  while ((elapsed = es_platform_cycles() - start) < duration) {
    uint32_t remaining = (duration - elapsed + ES_HZ - 1) / ES_HZ;
    struct timespec delay = {.tv_sec = remaining / 1000000u,
                            .tv_nsec = (long)(remaining % 1000000u) * 1000};
    if (nanosleep(&delay, NULL) < 0 && errno != EINTR)
      return -1;
  }
  return 0;
}

static int es_gate_release(uint32_t *epoch) {
  if (!gates_ready || released) {
    return gates_ready ? 0 : -1;
  }
  int result = 0;
  for (unsigned i = ready_consumed; i < spawned; i++) {
    while (sem_wait(&ready_gate) < 0) {
      if (errno != EINTR) {
        /* Still open the gate so an error cannot strand workers. */
        result = -1;
        break;
      }
    }
  }
  ready_consumed = spawned;
  if (epoch) {
    *epoch = es_platform_cycles();
  }
  released = 1;
  for (unsigned i = 0; i < spawned; i++) {
    if (sem_post(&go_gate) < 0) {
      return -1;
    }
  }
  return result;
}

static void es_close_queues(void) {
  for (unsigned s = 0; s < ES_SERVICES; s++) {
    for (unsigned q = 0; q < ES_QUEUES_PER_SERVICE; q++) {
      if (queue_opened[s][q]) {
        (void)mq_close(queue_handles[s][q]);
        (void)mq_unlink(queue_names[s][q]);
        queue_opened[s][q] = 0;
        queue_names[s][q][0] = '\0';
      }
      poll_sets[s][q] = (struct pollfd){.fd = -1, .events = POLLIN};
    }
  }
}

int es_platform_init(void) {
#ifdef __XTENSA__
  es_clock_prepare();
#endif
  if (initialized) {
    return -1;
  }
  memset(queue_names, 0, sizeof(queue_names));
  memset(queue_opened, 0, sizeof(queue_opened));
  memset(roles, 0, sizeof(roles));
  memset(thread_joined, 0, sizeof(thread_joined));
  memset(ready_cursor, 0, sizeof(ready_cursor));
  memset(poll_sets, 0, sizeof(poll_sets));
  spawned = 0;
  ready_consumed = 0;
  released = 0;
  spawn_failed = 0;
  atomic_store(&queue_peak, 0);

  if (sem_init(&ready_gate, 0, 0) < 0) {
    return -1;
  }
  if (sem_init(&go_gate, 0, 0) < 0) {
    (void)sem_destroy(&ready_gate);
    return -1;
  }
  gates_ready = 1;

  unsigned invocation = atomic_fetch_add(&invocation_id, 1u);
  const char *platform = "nuttx-c";
#ifdef ES_RUST
  platform = "nuttx-rust";
#endif
  struct mq_attr attr = {0};
  attr.mq_maxmsg = ES_QUEUE_SLOTS;
  attr.mq_msgsize = sizeof(struct es_event);
  for (unsigned s = 0; s < ES_SERVICES; s++) {
    for (unsigned q = 0; q < ES_QUEUES_PER_SERVICE; q++) {
      unsigned index = s * ES_QUEUES_PER_SERVICE + q;
      int n = snprintf(queue_names[s][q], sizeof(queue_names[s][q]),
                       "/es_%s_%ld_%u_%u", platform, (long)getpid(), invocation,
                       index);
      if (n < 0 || (size_t)n >= sizeof(queue_names[s][q])) {
        es_close_queues();
        (void)sem_destroy(&go_gate);
        (void)sem_destroy(&ready_gate);
        gates_ready = 0;
        return -1;
      }
      queue_handles[s][q] =
          mq_open(queue_names[s][q], O_CREAT | O_EXCL | O_RDWR | O_NONBLOCK,
                  0600, &attr);
      if (queue_handles[s][q] == (mqd_t)-1) {
        es_close_queues();
        (void)sem_destroy(&go_gate);
        (void)sem_destroy(&ready_gate);
        gates_ready = 0;
        return -1;
      }
      queue_opened[s][q] = 1;
      poll_sets[s][q] =
          (struct pollfd){.fd = (int)queue_handles[s][q], .events = POLLIN};
    }
  }
  initialized = 1;
  return 0;
}

void es_platform_close(void) {
  if (!initialized && !gates_ready) {
    return;
  }
  (void)es_platform_join();
  if (spawned != 0) {
    return;
  }
  es_close_queues();
  if (gates_ready) {
    (void)sem_destroy(&go_gate);
    (void)sem_destroy(&ready_gate);
    gates_ready = 0;
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
  if (mq_send(queue_handles[destination][q], (const char *)event,
              sizeof(*event), 0) == 0) {
    struct mq_attr attr;
    if (mq_getattr(queue_handles[destination][q], &attr) == 0) {
      unsigned seen = (unsigned)attr.mq_curmsgs;
      unsigned peak = atomic_load(&queue_peak);
      while (seen > peak &&
             !atomic_compare_exchange_weak(&queue_peak, &peak, seen)) {
      }
    }
    return 0;
  }
  return errno == EAGAIN ? 1 : -1;
}

int es_platform_wait(unsigned service, uint32_t timeout_cycles,
                     struct es_event *event) {
  if (!initialized || !event || service >= ES_SERVICES) {
    return -1;
  }
  unsigned count = ES_QUEUES_PER_SERVICE;
  uint64_t ms = ((uint64_t)timeout_cycles + 239999u) / 240000u;
  int timeout = ms > INT_MAX ? INT_MAX : (int)ms;
  int ready = poll(poll_sets[service], count, timeout);
  if (ready < 0) {
    return errno == EINTR ? 0 : -1;
  }
  if (ready == 0) {
    return 0;
  }
  unsigned cursor = ready_cursor[service];
  for (unsigned offset = 0; offset < count; offset++) {
    unsigned q = (cursor + offset) % count;
    short flags = poll_sets[service][q].revents;
    if (flags & POLLIN) {
      ssize_t got = mq_receive(queue_handles[service][q], (char *)event,
                               sizeof(*event), NULL);
      ready_cursor[service] = (q + 1u) % count;
      if (got == (ssize_t)sizeof(*event)) {
        return 1;
      }
      return got < 0 && errno == EAGAIN ? 0 : -1;
    }
    if (flags & (POLLERR | POLLNVAL | POLLHUP)) {
      return -1;
    }
  }
  return 0;
}

uint32_t es_platform_cycles(void) {
#if defined(__NuttX__) && defined(__XTENSA__)
  return es_clock_cycles();
#else
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
    return 0;
  }
  uint64_t cycles = (uint64_t)now.tv_sec * 240000000ull +
                    (uint64_t)now.tv_nsec * 240ull / 1000ull;
  return (uint32_t)cycles;
#endif
}

int es_platform_spawn(void *(*entry)(void *)) {
  if (!initialized || !entry || spawned >= ES_SERVICES || released) {
    return -1;
  }
  unsigned slot = spawned;
  roles[slot].entry = entry;
  roles[slot].argument = (void *)(uintptr_t)slot;
  pthread_attr_t attr;
  if (pthread_attr_init(&attr) != 0) {
    spawn_failed = 1;
    return -1;
  }
  size_t stack_bytes = ES_STACK_BYTES;
#ifndef __NuttX__
  stack_bytes = ES_HOST_STACK_BYTES;
#ifdef PTHREAD_STACK_MIN
  if (stack_bytes < PTHREAD_STACK_MIN) {
    stack_bytes = PTHREAD_STACK_MIN;
  }
#endif
#endif
  int rc = pthread_attr_setstacksize(&attr, stack_bytes);
  if (rc == 0) {
    rc = pthread_create(&threads[slot], &attr, entry, roles[slot].argument);
  }
  (void)pthread_attr_destroy(&attr);
  if (rc != 0) {
    spawn_failed = 1;
    return -1;
  }
  spawned++;
  return 0;
}

int es_platform_arrive(void) {
  if (!initialized || !gates_ready || released) {
    return -1;
  }
  if (sem_post(&ready_gate) < 0) {
    return -1;
  }
  while (sem_wait(&go_gate) < 0) {
    if (errno != EINTR) {
      return -1;
    }
  }
  return 0;
}

int es_platform_wait_ready(void) {
  if (!initialized || !gates_ready || released) {
    return -1;
  }
  for (unsigned i = ready_consumed; i < spawned; i++) {
    while (sem_wait(&ready_gate) < 0) {
      if (errno != EINTR) {
        return -1;
      }
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

int es_platform_join(void) {
  if (!initialized) {
    return -1;
  }
  int rc = es_gate_release(NULL);
  int all_joined = 1;
  for (unsigned i = 0; i < spawned; i++) {
    if (!thread_joined[i] && pthread_join(threads[i], NULL) == 0) {
      thread_joined[i] = 1;
    }
    if (!thread_joined[i]) {
      all_joined = 0;
      rc = -1;
    }
  }
  if (all_joined) {
    spawned = 0;
  }
  return rc == 0 && !spawn_failed ? 0 : -1;
}

uint32_t es_platform_heap_used(void) {
#ifdef __NuttX__
  return (uint32_t)mallinfo().uordblks;
#else
  return 0;
#endif
}

int es_platform_queue_depth(unsigned destination, unsigned kind) {
  if (!initialized || destination >= ES_SERVICES || kind >= ES_KINDS) {
    return -1;
  }
  struct mq_attr attr;
  if (mq_getattr(queue_handles[destination][es_qindex(kind)], &attr) < 0 ||
      attr.mq_curmsgs < 0 || attr.mq_curmsgs > INT_MAX) {
    return -1;
  }
  return (int)attr.mq_curmsgs;
}

unsigned es_platform_queue_peak(void) { return atomic_load(&queue_peak); }

void es_platform_resources(void) {
  size_t queue_objects = sizeof(queue_handles) + sizeof(queue_names) +
                         sizeof(queue_opened) + sizeof(poll_sets) +
                         sizeof(ready_cursor);
  size_t thread_objects = sizeof(threads) + sizeof(thread_joined);
#ifdef __NuttX__
  size_t stack_storage = (size_t)ES_SERVICES * ES_STACK_BYTES;
#else
  size_t stack_storage = (size_t)ES_SERVICES * ES_HOST_STACK_BYTES;
#endif
  printf("ES_RESOURCES queue_buffers=30720 queue_objects=%zu "
         "thread_objects=%zu entry_storage=%zu stack_storage=%zu "
         "heap_allocated=%u "
         "diagnostics_queue_peaks=%zu "
         "note=queue_peak_observation_may_underestimate\n",
         queue_objects, thread_objects, sizeof(roles), stack_storage,
         (unsigned)es_platform_heap_used(), sizeof(queue_peak));
}

const char *es_platform_name(void) {
#ifdef ES_RUST
  return "nuttx-rust";
#else
  return "nuttx-c";
#endif
}
