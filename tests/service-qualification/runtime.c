/* SPDX-License-Identifier: MIT
 * A local qualification adapter, not a production runtime. Queue admission
 * is nonblocking; an owner retains one unsent event and waits for downstream
 * writability together with its reserved stop queue. No handler blocks sending.
 */
#include "qualification.h"
#include <errno.h>
#include <fcntl.h>
#include <mqueue.h>
#include <poll.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#ifdef __NuttX__
#include <malloc.h>
#include <stdbool.h>
#include <sys/ioctl.h>
#include <nuttx/ioexpander/gpio.h>
#include "clock.h"
#include "pulse_snapshot.h"
#endif

int nxrs_cq_thread_start(void **, size_t, void *(*)(void *), void *);
int nxrs_cq_thread_join(void *, void **);
int es_hal_init(void);
int es_hal_apply(unsigned);
int es_hal_close(void);

struct sq_service {
  unsigned id, count;
  mqd_t queues[SQ_CLASSES];
  void *thread;
  sem_t *ready, *go;
  atomic_uint received, failures, done;
  unsigned started, announced, opened;
  uint32_t checksum, maximum_cycles, deadline_misses;
  uint64_t sum_cycles;
};
/* A failed shutdown can outlive the command task. Keep all borrowed worker
 * state, including startup gates and the original queue namespace, together. */
struct sq_run {
  sem_t ready, go;
  pid_t owner;
  unsigned count, released, stop_mask, ready_initialized, go_initialized;
  struct sq_service owners[];
};
static struct sq_run *run;
static struct sq_service *services;
#ifdef __NuttX__
static struct sq_pulse_snapshot pulse = SQ_PULSE_SNAPSHOT_INITIALIZER;
#endif
static unsigned irq_mode;
static int irq_fd = -1;
static int gpio_input_open(void);
static void gpio_input_close(void);
#ifdef __NuttX__
static int gpio_pulse_trigger(void *context) {
  return ioctl(*(int *)context, GPIOC_WRITE, 1);
}
static int gpio_pulse_acknowledge(void *context) {
  int fd = *(int *)context;
  char value;
  return lseek(fd, 0, SEEK_SET) < 0 || read(fd, &value, 1) != 1 ? -1 : 0;
}
#endif

static unsigned capacity(unsigned cls) {
  return cls == SQ_STOP ? SQ_STOP_CAPACITY : SQ_CAPACITY;
}

uint32_t es_platform_cycles(void) {
#ifdef __XTENSA__
  return es_clock_cycles();
#else
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  return (uint32_t)((uint64_t)now.tv_sec * 240000000u +
                    (uint64_t)now.tv_nsec * 24u / 100u);
#endif
}
static unsigned heap_used(void) {
#ifdef __NuttX__
  return mallinfo().uordblks;
#else
  return 0; /* Host functional tests do not claim firmware RAM measurements. */
#endif
}
static void queue_name(char *name, size_t size, unsigned id, unsigned cls) {
  snprintf(name, size, "/sq_%ld_%u_%u", (long)run->owner, id, cls);
}
static int send_event(unsigned id, const struct sq_event *event) {
  if (mq_send(services[id].queues[event->kind], (const char *)event,
              sizeof(*event), 0) == 0) return 0;
  return errno == EAGAIN ? 1 : -1;
}
unsigned nxrs_sq_id(const struct sq_service *service) { return service->id; }
unsigned nxrs_sq_count(const struct sq_service *service) { return service->count; }
void nxrs_sq_failed(struct sq_service *service) {
  atomic_fetch_add_explicit(&service->failures, 1, memory_order_relaxed);
}
void nxrs_sq_ready(struct sq_service *service) {
  service->announced = 1;
  sem_post(service->ready);
  while (sem_wait(service->go) < 0 && errno == EINTR) {}
}
int nxrs_sq_led_open(struct sq_service *service) {
  (void)service;
  return es_hal_init();
}
int nxrs_sq_led_apply(struct sq_service *service, unsigned level) {
  (void)service;
  return es_hal_apply(level);
}
int nxrs_sq_led_close(struct sq_service *service) {
  (void)service;
  return es_hal_close();
}

/* One blocking wait point. Control precedes data; stop has its own capacity.
 * GPIO is a native driver readiness event, never a std/MQ call in an ISR. */
int nxrs_sq_wait(struct sq_service *service, const struct sq_event *pending,
            struct sq_event *event) {
  for (;;) {
    struct pollfd waiters[SQ_CLASSES + 1] = {0};
    unsigned length = SQ_CLASSES;
    if (pending) {
      int sent = send_event(service->id + 1, pending);
      if (sent < 0) return -1;
      if (sent == 0) pending = NULL;
    }
    for (unsigned cls = 0; cls < SQ_CLASSES; cls++) {
      waiters[cls].fd = (int)service->queues[cls];
      waiters[cls].events = cls == SQ_STOP || !pending ? POLLIN : 0;
    }
    if (pending) {
      waiters[length].fd = (int)services[service->id + 1].queues[pending->kind];
      waiters[length++].events = POLLOUT;
    } else if (irq_mode && service->id == 0) {
      waiters[length].fd = irq_fd;
      waiters[length++].events = POLLIN;
    }
    int result = poll(waiters, length, -1);
    if (result < 0) { if (errno == EINTR) continue; return -1; }
    for (unsigned index = 0; index < length; index++)
      if (waiters[index].revents & (POLLERR | POLLHUP | POLLNVAL)) return -1;
    for (unsigned cls = 0; cls < SQ_CLASSES; cls++) {
      if (!(waiters[cls].revents & POLLIN)) continue;
      ssize_t size = mq_receive(service->queues[cls], (char *)event, sizeof(*event), NULL);
      if (size < 0 && (errno == EAGAIN || errno == EINTR)) continue;
      if (size != sizeof(*event) || event->kind != cls) return -1;
      if (cls == SQ_STOP) return pending ? -1 : 0;
      return 1;
    }
#ifdef __NuttX__
    if (!pending && irq_mode && service->id == 0 &&
        waiters[SQ_CLASSES].revents & POLLIN) {
      /* Acknowledge and copy under the same task-context guard as the rising
       * edge. A newer publication cannot tear or overtake this snapshot.
       * This is still one latest pulse, not an edge buffer: coalescing fails
       * the ordinary sequence/count checks rather than manufacturing events. */
      if (sq_pulse_receive(&pulse, event, gpio_pulse_acknowledge, &irq_fd) != 0) return -1;
      return 1;
    }
#endif
  }
}
void nxrs_sq_record(struct sq_service *service, const struct sq_event *event) {
  service->checksum += event->sequence;
  if (service->id == service->count - 2) {
    uint32_t elapsed = es_platform_cycles() - event->origin_cycles;
    service->sum_cycles += elapsed;
    if (elapsed > service->maximum_cycles) service->maximum_cycles = elapsed;
    if (elapsed > 240000u) service->deadline_misses++; /* Declared 1 ms target. */
  }
  atomic_fetch_add_explicit(&service->received, 1, memory_order_release);
}
static void *entry(void *argument) {
  struct sq_service *service = argument;
  if (irq_mode && service->id == 0 && gpio_input_open() != 0) nxrs_sq_failed(service);
  else nxrs_sq_worker(service);
  if (irq_mode && service->id == 0) gpio_input_close();
  if (!service->announced) sem_post(service->ready);
  atomic_store_explicit(&service->done, 1, memory_order_release);
  return NULL;
}
static int any_failure(unsigned count) {
  for (unsigned id = 0; id < count; id++)
    if (atomic_load_explicit(&services[id].failures, memory_order_relaxed)) return 1;
  return 0;
}
static int queues_full(unsigned count, unsigned *peak) {
  struct sq_event event = {0}, received;
  unsigned filled = 0;
  for (unsigned id = 0; id < count; id++) {
    for (unsigned cls = 0; cls < SQ_CLASSES; cls++) {
      event.kind = cls;
      for (unsigned slot = 0; slot < capacity(cls); slot++) {
        event.sequence = slot;
        if (send_event(id, &event) != 0) return -1;
        filled++;
      }
      if (send_event(id, &event) != 1) return -1;
    }
  }
  *peak = heap_used();
  for (unsigned id = 0; id < count; id++)
    for (unsigned cls = 0; cls < SQ_CLASSES; cls++)
      for (unsigned slot = 0; slot < capacity(cls); slot++) {
        if (mq_receive(services[id].queues[cls], (char *)&received,
                       sizeof(received), NULL) != sizeof(received) ||
            received.kind != cls || received.sequence != slot) return -1;
      }
  return filled == count * SQ_SLOTS ? 0 : -1;
}
static int gpio_input_open(void) {
#ifdef __NuttX__
  irq_fd = open("/dev/gpio2", O_RDONLY);
  if (irq_fd < 0) return -1;
  if (ioctl(irq_fd, GPIOC_REGISTER, 0) < 0) { close(irq_fd); irq_fd = -1; return -1; }
  return 0;
#else
  return -1;
#endif
}
static void gpio_input_close(void) {
#ifdef __NuttX__
  if (irq_fd >= 0) { ioctl(irq_fd, GPIOC_UNREGISTER, 0); close(irq_fd); irq_fd = -1; }
#endif
}

static void release_run(void) {
  if (run->go_initialized) sem_destroy(&run->go);
  if (run->ready_initialized) sem_destroy(&run->ready);
  free(run);
  run = NULL;
  services = NULL;
}

static void report_pending(void) {
  unsigned pending = 0, queues = 0;
  for (unsigned id = 0; id < run->count; id++) {
    pending += services[id].started != 0;
    queues += services[id].opened;
  }
  printf("SQ_CLEANUP pending_threads=%u retained_queues=%u\n", pending, queues);
}

/* Return incomplete rather than join a worker whose stop was not delivered.
 * A failed join never relinquishes ownership. No queues, gates or contexts
 * are destroyed until every started worker has been successfully joined. */
static int shutdown_run(int *result) {
  if (!run->released) {
    for (unsigned id = 0; id < run->count; id++)
      if (services[id].started) sem_post(&run->go);
    run->released = 1;
  }
  for (unsigned id = 0; id < run->count; id++) {
    struct sq_service *service = &services[id];
    unsigned bit = 1u << id;
    if (!service->started || (run->stop_mask & bit) ||
        atomic_load_explicit(&service->done, memory_order_acquire)) continue;
    struct sq_event stop = {.kind = SQ_STOP};
    int sent = -1;
    if (service->opened == SQ_CLASSES) {
      /* Interrupted sends get a bounded retry; permanent errors cannot spin.
       * EAGAIN on the dedicated stop inbox means a stop is already queued. */
      for (unsigned attempt = 0; attempt < 3; attempt++) {
        sent = send_event(id, &stop);
        if (sent >= 0 || errno != EINTR) break;
      }
    }
    if (sent >= 0) run->stop_mask |= bit;
    else *result = 1;
  }
  unsigned pending = 0;
  for (unsigned id = 0; id < run->count; id++) {
    struct sq_service *service = &services[id];
    if (!service->started) continue;
    if (((run->stop_mask & (1u << id)) ||
         atomic_load_explicit(&service->done, memory_order_acquire)) &&
        nxrs_cq_thread_join(service->thread, NULL) == 0) {
      service->started = 0;
      service->thread = NULL;
    } else {
      pending++;
      *result = 1;
    }
  }
  if (pending) {
    report_pending();
    return -1;
  }
  for (unsigned id = 0; id < run->count; id++) {
    struct sq_service *service = &services[id];
    if (atomic_load_explicit(&service->failures, memory_order_relaxed)) *result = 1;
    for (unsigned cls = 0; cls < service->opened; cls++) {
      char name[40]; queue_name(name, sizeof(name), id, cls);
      /* Close and unlink are independent attempts. Do not retry a released or
       * invalid descriptor, which may be reused; always attempt name removal. */
      if (mq_close(service->queues[cls]) != 0) *result = 1;
      service->queues[cls] = (mqd_t)-1;
      if (mq_unlink(name) != 0) *result = 1;
    }
  }
  gpio_input_close();
  return 0;
}

int nxrs_sq_run(int argc, char **argv) {
  unsigned count = 3, events = 1000, period_us = 2000, peak = 0, admitted = 0;
  int result = 1, fixture_fd = -1;
  if (argc > 1) count = (unsigned)strtoul(argv[1], NULL, 10);
  if (argc > 2) events = (unsigned)strtoul(argv[2], NULL, 10);
  if (argc > 3) period_us = (unsigned)strtoul(argv[3], NULL, 10);
  if (count < 3 || count > SQ_MAX_SERVICES || events < 1 || events > 100000 ||
      period_us < 100 || period_us > 100000) return 2;
  if (run != NULL) {
    /* NuttX app tasks may have distinct descriptor tables. A later task must
     * not close or join a failed owner's handles; recover with the original
     * coordinator, otherwise restart the application domain. */
    if (run->owner != getpid()) { report_pending(); goto finish; }
    int previous = 0;
    if (shutdown_run(&previous) != 0) goto finish;
    release_run();
    if (previous != 0) goto finish;
  }
  irq_mode = argc > 4 && strcmp(argv[4], "gpio") == 0;
#ifdef __XTENSA__
  es_clock_prepare();
#endif
  unsigned before = heap_used();
  run = calloc(1, sizeof(*run) + count * sizeof(*services));
  if (!run) goto finish;
  services = run->owners;
  run->owner = getpid();
  run->count = count;
  if (sem_init(&run->ready, 0, 0) != 0) { release_run(); goto finish; }
  run->ready_initialized = 1;
  if (sem_init(&run->go, 0, 0) != 0) { release_run(); goto finish; }
  run->go_initialized = 1;
  for (unsigned id = 0; id < count; id++) {
    struct sq_service *service = &services[id];
    service->id = id; service->count = count; service->ready = &run->ready; service->go = &run->go;
    atomic_init(&service->received, 0); atomic_init(&service->failures, 0); atomic_init(&service->done, 0);
  }
  for (unsigned id = 0; id < count; id++) {
    struct sq_service *service = &services[id];
    for (unsigned cls = 0; cls < SQ_CLASSES; cls++) {
      char name[40];
      queue_name(name, sizeof(name), id, cls);
      struct mq_attr attributes = {.mq_maxmsg = capacity(cls), .mq_msgsize = sizeof(struct sq_event)};
      service->queues[cls] = mq_open(name, O_CREAT | O_EXCL | O_RDWR | O_NONBLOCK,
                                     0600, &attributes);
      if (service->queues[cls] == (mqd_t)-1) goto cleanup;
      service->opened++;
    }
  }
#ifdef __NuttX__
  if (irq_mode) {
    fixture_fd = open("/dev/gpio0", O_WRONLY);
    if (fixture_fd < 0 || ioctl(fixture_fd, GPIOC_WRITE, 0) < 0) goto cleanup;
  }
#endif
  for (unsigned id = 0; id < count; id++) {
#ifdef __NuttX__
    size_t stack = SQ_STACK;
#else
    size_t stack = 65536; /* Host pthread minimum differs; never a target RAM result. */
#endif
    if (nxrs_cq_thread_start(&services[id].thread, stack, entry, &services[id]) != 0) goto cleanup;
    services[id].started = 1;
  }
  for (unsigned id = 0; id < count; id++)
    while (sem_wait(&run->ready) < 0 && errno == EINTR) {}
  if (any_failure(count) || queues_full(count, &peak) != 0) goto cleanup;
  printf("SQ_MEMORY before=%u full=%u delta=%u metadata=%u payload=%u stacks=%u\n",
         before, peak, peak - before, (unsigned)(count * sizeof(*services)),
         (unsigned)(count * SQ_SLOTS * sizeof(struct sq_event)), count * SQ_STACK);
  for (unsigned id = 0; id < count; id++) sem_post(&run->go);
  run->released = 1;
  for (unsigned sequence = 0; sequence < events; sequence++) {
    if (any_failure(count)) goto cleanup;
    struct sq_event event = {.origin_cycles = es_platform_cycles(),
      .sequence = sequence, .value = sequence & 1u,
      .kind = sequence % 3u == 0 ? SQ_CONTROL : SQ_DATA};
    if (irq_mode) {
#ifdef __NuttX__
      if (sq_pulse_publish(&pulse, &event, gpio_pulse_trigger, &fixture_fd) != 0) goto cleanup;
      admitted++;
      usleep(period_us / 2);
      if (ioctl(fixture_fd, GPIOC_WRITE, 0) < 0) goto cleanup;
      usleep(period_us / 2);
#endif
    } else {
      /* Explicit backpressure, with a bounded producer retry, not silent loss. */
      uint32_t start = es_platform_cycles();
      for (;;) {
        int sent = send_event(0, &event);
        if (sent < 0) goto cleanup;
        if (sent == 0) { admitted++; break; }
        if ((uint32_t)(es_platform_cycles() - start) > 240000000u) goto cleanup;
        usleep(100);
      }
      usleep(period_us);
    }
  }
  {
    uint32_t start = es_platform_cycles();
    while (atomic_load_explicit(&services[count - 1].received, memory_order_acquire) != events) {
      if (any_failure(count) || (uint32_t)(es_platform_cycles() - start) > 240000000u) goto cleanup;
      usleep(1000);
    }
  }
  result = 0;
cleanup:
  /* Quiesce the external source first, then stop/join every started owner.
   * Stop has reserved capacity, including partial-startup/failure cleanup. */
  if (fixture_fd >= 0) { close(fixture_fd); fixture_fd = -1; }
  if (shutdown_run(&result) != 0) goto finish;
  for (unsigned id = 0; id < count; id++) {
    struct sq_service *service = &services[id];
    if (result == 0 && (atomic_load_explicit(&service->received, memory_order_relaxed) != events ||
        service->checksum != (uint32_t)((uint64_t)events * (events - 1u) / 2u))) result = 1;
  }
  if (admitted == events) {
    struct sq_service *led = &services[count - 2];
    printf("SQ_RESULT services=%u queues=%u events=%u received=%u errors=%u mean_cycles=%llu max_cycles=%u misses_1ms=%u source=%s\n",
           count, count * SQ_CLASSES, admitted,
           atomic_load_explicit(&services[count - 1].received, memory_order_relaxed), result,
           (unsigned long long)(led->sum_cycles / events), (unsigned)led->maximum_cycles,
           (unsigned)led->deadline_misses, irq_mode ? "gpio" : "messages");
  }
  release_run();
finish:
  printf("SQ_DONE status=%d heap_after=%u\n", result, heap_used());
  return result;
}
