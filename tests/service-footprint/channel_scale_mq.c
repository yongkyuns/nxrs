/* SPDX-License-Identifier: MIT
 * Finite POSIX-mqueue control for cq-scale. Queue counts, payload, traffic,
 * ordering checks, stacks, and cycle samples match the Rust fixture.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <mqueue.h>
#include <poll.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#if defined(NXRS_CQ_SPEED) && defined(__GNUC__) && !defined(__clang__)
/* Application-only speed control; the kernel and common target helpers keep Os. */
#pragma GCC optimize ("O2")
#endif
#ifdef NXRS_CQ_PACKET_SERVICE
#  include "pipeline_processing.h"
#endif


#define PRODUCERS 4
#define MAX_WORKERS 15
#ifdef NXRS_CQ_COMPACT_TOPOLOGY
#  define MAX_LANES 55
#  define LARGE_WORKERS 5
#  define MAX_LANES_PER_WORKER 11
#  define MAX_SAMPLES 880
#else
#  define MAX_LANES 45
#  define LARGE_WORKERS 15
#  define MAX_LANES_PER_WORKER 3
#  define MAX_SAMPLES 720
#endif
#define MAX_QUEUES 60
#define SAMPLE_EVERY 8
#define WAKE_WARMUP 4
#define WAKE_TRIALS 64
#if defined(NXRS_CQ_SILENT_LED_PRODUCTION) && \
    !defined(NXRS_CQ_LED_PRODUCTION)
#  error "silent LED fixture requires production mode"
#endif
static atomic_uint threads_ready;
static atomic_bool threads_release;
#ifdef NXRS_CQ_DEVICE
#  define WORKER_STACK 4096
#  define COLLECTOR_STACK 6144
extern uint32_t nxrs_cq_cycles(void);
extern uint32_t nxrs_cq_stack_hwm(void);
extern uint32_t nxrs_cq_heap_used(void);
#else
#  define WORKER_STACK 65536
#  define COLLECTOR_STACK 65536
#endif
#ifdef NXRS_CQ_SYNCHRONIZED
extern int nxrs_cq_gate_init(unsigned count);
extern int nxrs_cq_gate_arrive(void);
extern int nxrs_cq_gate_release(void);
extern void nxrs_cq_gate_done(void);
extern uint32_t nxrs_cq_gate_elapsed(void);
extern uint32_t nxrs_cq_gate_heap_ready(void);
extern int nxrs_cq_gate_destroy(void);
#endif

struct event
{
  uint8_t lane;
  uint8_t producer;
  uint16_t sequence;
  uint32_t sent_cycles;
  uint32_t checksum;
  uint8_t payload[236];
};

struct ack
{
  uint8_t lane;
  uint8_t producer;
  uint16_t sequence;
  uint32_t checksum;
  uint32_t sent_cycles;
  uint32_t received_cycles;
#ifdef NXRS_CQ_PACKET_SERVICE
  int32_t filtered[3];
#endif
};

_Static_assert(sizeof(struct event) == 248, "event layout must match Rust");
#ifdef NXRS_CQ_PACKET_SERVICE
_Static_assert(sizeof(struct ack) == 28, "pipeline ack layout must be 28 bytes");
#else
_Static_assert(sizeof(struct ack) == 16, "ack layout must match Rust");
#endif

struct queue
{
  mqd_t fd;
  char name[32];
};

struct config
{
  const char *label;
  unsigned workers;
  unsigned lanes_per_worker;
  unsigned sequences;
};

struct scenario;
struct producer_task
{
  struct scenario *scenario;
  unsigned id;
  const char *error;
  uint32_t stack_hwm;
#ifdef NXRS_CQ_PROFILE_PHASES
  uint64_t build_cycles;
  uint64_t send_cycles;
#endif
};

struct worker_task
{
  struct scenario *scenario;
  unsigned id;
  const char *error;
  uint32_t stack_hwm;
#ifdef NXRS_CQ_PROFILE_PHASES
  uint64_t poll_cycles;
  uint64_t receive_cycles;
  uint64_t validate_cycles;
  uint64_t send_cycles;
#endif
};

struct collector_task
{
  struct scenario *scenario;
  const char *error;
  uint32_t stack_hwm;
  unsigned received;
  uint32_t digest;
  uint32_t inbound[MAX_SAMPLES];
  uint32_t outbound[MAX_SAMPLES];
  unsigned samples;
#ifdef NXRS_CQ_PROFILE_PHASES
  uint64_t poll_cycles;
  uint64_t receive_cycles;
  uint64_t validate_cycles;
#endif
};

struct scenario
{
  struct config config;
  struct queue inputs[MAX_LANES];
  struct queue outputs[MAX_WORKERS];
  struct producer_task producers[PRODUCERS];
  struct worker_task workers[MAX_WORKERS];
  struct collector_task collector;
};

static uint32_t cycles(void)
{
#ifdef NXRS_CQ_DEVICE
  return nxrs_cq_cycles();
#else
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  uint64_t nanoseconds = (uint64_t)now.tv_sec * 1000000000u + now.tv_nsec;
  return (uint32_t)(nanoseconds * 240u / 1000u);
#endif
}


static uint32_t stack_hwm(void)
{
#ifdef NXRS_CQ_DEVICE
  return nxrs_cq_stack_hwm();
#else
  return 0;
#endif
}

static uint32_t micros(uint32_t delta)
{
  return (delta + 239u) / 240u;
}

static uint64_t monotonic_us(void)
{
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  return (uint64_t)now.tv_sec * 1000000u + now.tv_nsec / 1000u;
}

static void sleep_ms(unsigned ms)
{
  struct timespec delay = {.tv_sec = ms / 1000u,
                           .tv_nsec = (long)(ms % 1000u) * 1000000l};
  while (nanosleep(&delay, &delay) < 0 && errno == EINTR)
    {
    }
}

static uint32_t checksum(uint8_t lane, uint8_t producer, uint16_t sequence,
                         const uint8_t *payload)
{
  uint32_t token = (uint32_t)lane << 24 | (uint32_t)producer << 16 | sequence;
#if defined(NXRS_CQ_WIRE_ONLY) || defined(NXRS_CQ_PACKET_SERVICE)
  (void)payload;
  return token ^ 0xa5a5a5a5u;
#else
  uint32_t sum = token;
  for (unsigned i = 0; i < 236; i++)
    {
      sum = (sum << 3 | sum >> 29) + payload[i];
    }
  return sum;
#endif
}

static int make_payload(uint8_t lane, uint8_t producer, uint16_t sequence,
                        uint8_t *payload)
{
#if defined(NXRS_CQ_PACKET_SERVICE) && !defined(NXRS_CQ_WIRE_ONLY)
  return nxrs_c_pipeline_build(payload, lane, producer, sequence);
#elif defined(NXRS_CQ_WIRE_ONLY)
  (void)lane;
  (void)producer;
  (void)sequence;
  memset(payload, 0xa5, 236);
#else
  for (unsigned i = 0; i < 236; i++)
    {
      payload[i] = (uint8_t)((uint8_t)(lane * 17u + producer +
                                       (uint8_t)sequence) ^ (uint8_t)i);
    }
#endif
  return 0;
}

static int make_event(struct event *event, unsigned lane, unsigned producer,
                      unsigned sequence)
{
  event->lane = lane;
  event->producer = producer;
  event->sequence = sequence;
  if (make_payload(event->lane, event->producer, event->sequence,
                   event->payload) != 0)
    {
      return -1;
    }
  event->checksum = checksum(event->lane, event->producer, event->sequence,
                             event->payload);
  event->sent_cycles = sequence % SAMPLE_EVERY == 0 ? cycles() : 0;
  return 0;
}

static unsigned lanes(const struct config *config)
{
  return config->workers * config->lanes_per_worker;
}

static unsigned input_queues(const struct config *config)
{
#ifdef NXRS_CQ_MULTIPLEXED
  return config->workers;
#else
  return lanes(config);
#endif
}

static unsigned events(const struct config *config)
{
  return lanes(config) * PRODUCERS * config->sequences;
}

static unsigned samples(const struct config *config)
{
  return lanes(config) * PRODUCERS *
         ((config->sequences + SAMPLE_EVERY - 1) / SAMPLE_EVERY);
}

static int create_queue(struct queue *queue, unsigned id, long capacity,
                        long message_size)
{
  struct mq_attr attr = {.mq_maxmsg = capacity, .mq_msgsize = message_size};
  snprintf(queue->name, sizeof(queue->name), "/cqs%04x%02x",
           (unsigned)(cycles() ^ (uint32_t)getpid()) & 0xffffu, id);
  queue->fd = mq_open(queue->name, O_CREAT | O_EXCL | O_RDWR, 0600, &attr);
  if (queue->fd < 0)
    {
      perror("mq_open");
      return -1;
    }
  return 0;
}

static void close_queue(struct queue *queue)
{
  if (queue->fd >= 0)
    {
      mq_close(queue->fd);
      mq_unlink(queue->name);
      queue->fd = -1;
    }
}

static int spawn(pthread_t *thread, void *(*entry)(void *), void *argument,
                 size_t stack_size)
{
  pthread_attr_t attr;
  if (pthread_attr_init(&attr) != 0)
    {
      return -1;
    }
  int result = pthread_attr_setstacksize(&attr, stack_size);
  if (result == 0)
    {
      result = pthread_create(thread, &attr, entry, argument);
    }
  pthread_attr_destroy(&attr);
  return result == 0 ? 0 : -1;
}


static void *idle_thread(void *argument)
{
  (void)argument;
  atomic_fetch_add_explicit(&threads_ready, 1, memory_order_release);
  while (!atomic_load_explicit(&threads_release, memory_order_acquire))
    {
      sleep_ms(1);
    }
  return NULL;
}

/* Exactly the large case's 20 spawned threads and stack reservations, with
 * no message queues or payload work. Keep all threads alive for the snapshot.
 */
static int run_thread_baseline(void)
{
  pthread_t threads[MAX_WORKERS + PRODUCERS + 2];
  unsigned started = 0;
  atomic_store(&threads_ready, 0);
  atomic_store(&threads_release, false);
  uint32_t heap_before = 0;
#ifdef NXRS_CQ_DEVICE
  heap_before = nxrs_cq_heap_used();
#endif
  if (spawn(&threads[started], idle_thread, NULL, COLLECTOR_STACK) != 0)
    {
      goto cleanup;
    }
  started++;
  for (unsigned i = 0; i < LARGE_WORKERS + PRODUCERS; i++)
    {
      if (spawn(&threads[started], idle_thread, NULL, WORKER_STACK) != 0)
        {
          goto cleanup;
        }
      started++;
    }
  uint64_t deadline = monotonic_us() + 5000000u;
  while (atomic_load_explicit(&threads_ready, memory_order_acquire) != started)
    {
      if (monotonic_us() >= deadline)
        {
          goto cleanup;
        }
      sleep_ms(1);
    }
  uint32_t heap_live = 0;
#ifdef NXRS_CQ_DEVICE
  heap_live = nxrs_cq_heap_used();
#endif
  atomic_store_explicit(&threads_release, true, memory_order_release);
  for (unsigned i = 0; i < started; i++)
    {
      pthread_join(threads[i], NULL);
    }
  uint32_t heap_after = 0;
#ifdef NXRS_CQ_DEVICE
  heap_after = nxrs_cq_heap_used();
#endif
  printf("CQ_C_THREADS_PASS threads=%u heap_before=%lu heap_live=%lu "
         "heap_after=%lu\n", started, (unsigned long)heap_before,
         (unsigned long)heap_live, (unsigned long)heap_after);
  return 0;
cleanup:
  atomic_store_explicit(&threads_release, true, memory_order_release);
  for (unsigned i = 0; i < started; i++)
    {
      pthread_join(threads[i], NULL);
    }
  fprintf(stderr, "CQ_C_THREADS_FAIL startup\n");
  return -1;
}

static int ready_index(struct pollfd *fds, unsigned count, unsigned *cursor)
{
  for (;;)
    {
      int result = poll(fds, count, -1);
      if (result < 0 && errno == EINTR)
        {
          continue;
        }
      if (result <= 0)
        {
          return -1;
        }
      for (unsigned offset = 0; offset < count; offset++)
        {
          unsigned index = (*cursor + offset) % count;
          if (fds[index].revents & POLLIN)
            {
              *cursor = (index + 1) % count;
              return index;
            }
          if (fds[index].revents & (POLLERR | POLLNVAL))
            {
              return -1;
            }
        }
    }
}

static void *producer_main(void *argument)
{
  struct producer_task *task = argument;
  struct scenario *scenario = task->scenario;
#ifdef NXRS_CQ_SYNCHRONIZED
  if (nxrs_cq_gate_arrive() != 0)
    {
      task->error = "transport gate arrive failed";
      return NULL;
    }
#endif
  for (unsigned sequence = 0; sequence < scenario->config.sequences;
       sequence++)
    {
      for (unsigned lane = 0; lane < lanes(&scenario->config); lane++)
        {
          struct event event;
#ifdef NXRS_CQ_PROFILE_PHASES
          uint32_t phase_started = cycles();
#endif
          if (make_event(&event, lane, task->id, sequence) != 0)
            {
              task->error = "event payload build failed";
              return NULL;
            }
#ifdef NXRS_CQ_PROFILE_PHASES
          task->build_cycles += cycles() - phase_started;
          phase_started = cycles();
#endif
#ifdef NXRS_CQ_MULTIPLEXED
          unsigned queue_index = lane / scenario->config.lanes_per_worker;
#else
          unsigned queue_index = lane;
#endif
          if (mq_send(scenario->inputs[queue_index].fd, (const char *)&event,
                      sizeof(event), 0) != 0)
            {
              task->error = "input mq_send failed";
              return NULL;
            }
#ifdef NXRS_CQ_PROFILE_PHASES
          task->send_cycles += cycles() - phase_started;
#endif
        }
    }
  task->stack_hwm = stack_hwm();
  return NULL;
}

static void *worker_main(void *argument)
{
  struct worker_task *task = argument;
  struct scenario *scenario = task->scenario;
  unsigned count = scenario->config.lanes_per_worker;
  struct pollfd fds[MAX_LANES_PER_WORKER];
  uint16_t next[MAX_LANES_PER_WORKER][PRODUCERS] = {{0}};
#if defined(NXRS_CQ_PACKET_SERVICE) && !defined(NXRS_CQ_WIRE_ONLY)
  int32_t pipeline_state[MAX_LANES_PER_WORKER][PRODUCERS][3] = {{{0}}};
#endif
  unsigned cursor = 0;
#ifdef NXRS_CQ_MULTIPLEXED
  unsigned fd_count = 1;
#else
  unsigned fd_count = count;
#endif
  for (unsigned i = 0; i < fd_count; i++)
    {
#ifdef NXRS_CQ_MULTIPLEXED
      unsigned input_index = task->id;
#else
      unsigned input_index = task->id * count + i;
#endif
      fds[i] = (struct pollfd){.fd = scenario->inputs[input_index].fd,
                               .events = POLLIN};
    }
  unsigned target = count * PRODUCERS * scenario->config.sequences;
#ifdef NXRS_CQ_SYNCHRONIZED
  if (nxrs_cq_gate_arrive() != 0)
    {
      task->error = "transport gate arrive failed";
      return NULL;
    }
#endif
  for (unsigned handled = 0; handled < target; handled++)
    {
#ifdef NXRS_CQ_PROFILE_PHASES
      uint32_t phase_started = cycles();
#endif
      int which = ready_index(fds, fd_count, &cursor);
#ifdef NXRS_CQ_PROFILE_PHASES
      task->poll_cycles += cycles() - phase_started;
      phase_started = cycles();
#endif
      struct event event;
      if (which < 0 || mq_receive(fds[which].fd, (char *)&event,
                                  sizeof(event), NULL) != sizeof(event))
        {
          task->error = "input poll or mq_receive failed";
          return NULL;
        }
#ifdef NXRS_CQ_PROFILE_PHASES
      task->receive_cycles += cycles() - phase_started;
      phase_started = cycles();
#endif
#ifdef NXRS_CQ_MULTIPLEXED
      unsigned local = event.lane - task->id * count;
#else
      unsigned local = (unsigned)which;
#endif
      if (local >= count || event.lane != task->id * count + local ||
          event.producer >= PRODUCERS ||
          event.sequence != next[local][event.producer] ||
#ifdef NXRS_CQ_WIRE_ONLY
          event.payload[0] != 0xa5 || event.payload[235] != 0xa5 ||
#endif
          event.checksum != checksum(event.lane, event.producer,
                                     event.sequence, event.payload))
        {
          task->error = "input payload or per-sender order invalid";
          return NULL;
        }
#ifdef NXRS_CQ_PACKET_SERVICE
      int32_t filtered[3] = {0, 0, 0};
#  ifndef NXRS_CQ_WIRE_ONLY
      uint32_t token = ((uint32_t)event.lane << 24) |
                       ((uint32_t)event.producer << 16) | event.sequence;
      if (nxrs_c_pipeline_process(event.payload, token,
                                  pipeline_state[local][event.producer],
                                  filtered) != 0)
        {
          task->error = "input packet processing failed";
          return NULL;
        }
#  endif
#endif
      next[local][event.producer]++;
      struct ack ack = {.lane = event.lane,
                        .producer = event.producer,
                        .sequence = event.sequence,
                        .checksum = event.checksum,
                        .sent_cycles = event.sent_cycles,
                        .received_cycles = event.sequence % SAMPLE_EVERY == 0
                            ? cycles() : 0};
#ifdef NXRS_CQ_PACKET_SERVICE
      memcpy(ack.filtered, filtered, sizeof(ack.filtered));
#endif
#ifdef NXRS_CQ_PROFILE_PHASES
      task->validate_cycles += cycles() - phase_started;
      phase_started = cycles();
#endif
      if (mq_send(scenario->outputs[task->id].fd, (const char *)&ack,
                  sizeof(ack), 0) != 0)
        {
          task->error = "output mq_send failed";
          return NULL;
        }
#ifdef NXRS_CQ_PROFILE_PHASES
      task->send_cycles += cycles() - phase_started;
#endif
    }
  for (unsigned i = 0; i < count; i++)
    {
      for (unsigned producer = 0; producer < PRODUCERS; producer++)
        {
          if (next[i][producer] != scenario->config.sequences)
            {
              task->error = "worker missed an input event";
              return NULL;
            }
        }
    }
  task->stack_hwm = stack_hwm();
  return NULL;
}

static void *collector_main(void *argument)
{
  struct collector_task *task = argument;
  struct scenario *scenario = task->scenario;
  struct pollfd fds[MAX_WORKERS];
  uint16_t next[MAX_LANES][PRODUCERS] = {{0}};
#if defined(NXRS_CQ_PACKET_SERVICE) && !defined(NXRS_CQ_WIRE_ONLY)
  int32_t pipeline_state[MAX_LANES][PRODUCERS][3] = {{{0}}};
#endif
#ifndef NXRS_CQ_WIRE_ONLY
  uint8_t payload[236];
#endif
  unsigned cursor = 0;
  for (unsigned i = 0; i < scenario->config.workers; i++)
    {
      fds[i] = (struct pollfd){.fd = scenario->outputs[i].fd,
                               .events = POLLIN};
    }
#ifdef NXRS_CQ_SYNCHRONIZED
  if (nxrs_cq_gate_arrive() != 0)
    {
      task->error = "transport gate arrive failed";
      return NULL;
    }
#endif
  for (unsigned received = 0; received < events(&scenario->config); received++)
    {
#ifdef NXRS_CQ_PROFILE_PHASES
      uint32_t phase_started = cycles();
#endif
      int which = ready_index(fds, scenario->config.workers, &cursor);
#ifdef NXRS_CQ_PROFILE_PHASES
      task->poll_cycles += cycles() - phase_started;
      phase_started = cycles();
#endif
      struct ack ack;
      if (which < 0 || mq_receive(fds[which].fd, (char *)&ack,
                                  sizeof(ack), NULL) != sizeof(ack))
        {
          task->error = "output poll or mq_receive failed";
          return NULL;
        }
#ifdef NXRS_CQ_PROFILE_PHASES
      task->receive_cycles += cycles() - phase_started;
      phase_started = cycles();
#endif
      uint32_t collected_cycles = ack.sequence % SAMPLE_EVERY == 0
                                  ? cycles() : 0;
      if (ack.lane >= lanes(&scenario->config) ||
          ack.producer >= PRODUCERS ||
          ack.lane / scenario->config.lanes_per_worker != (unsigned)which ||
          ack.sequence != next[ack.lane][ack.producer])
        {
          task->error = "outbound event missing, duplicated, or out of order";
          return NULL;
        }
#ifndef NXRS_CQ_WIRE_ONLY
      if (make_payload(ack.lane, ack.producer, ack.sequence, payload) != 0)
        {
          task->error = "outbound payload rebuild failed";
          return NULL;
        }
#endif
      if (ack.checksum != checksum(ack.lane, ack.producer,
#ifdef NXRS_CQ_WIRE_ONLY
                                   ack.sequence, NULL))
#else
                                   ack.sequence, payload))
#endif
        {
          task->error = "outbound checksum mismatch";
          return NULL;
        }
#ifdef NXRS_CQ_PACKET_SERVICE
#  ifndef NXRS_CQ_WIRE_ONLY
      int32_t expected_filtered[3];
      uint32_t token = ((uint32_t)ack.lane << 24) |
                       ((uint32_t)ack.producer << 16) | ack.sequence;
      if (nxrs_c_pipeline_process(payload, token,
                                  pipeline_state[ack.lane][ack.producer],
                                  expected_filtered) != 0 ||
          memcmp(ack.filtered, expected_filtered,
                 sizeof(expected_filtered)) != 0)
        {
          task->error = "outbound packet result mismatch";
          return NULL;
        }
#  else
      if (ack.filtered[0] != 0 || ack.filtered[1] != 0 ||
          ack.filtered[2] != 0)
        {
          task->error = "wire-only acknowledgment payload was not zero";
          return NULL;
        }
#  endif
#endif
      next[ack.lane][ack.producer]++;
      task->received++;
      task->digest += ack.checksum;
      if (ack.sequence % SAMPLE_EVERY == 0)
        {
          if (task->samples >= MAX_SAMPLES)
            {
              task->error = "sample capacity exceeded";
              return NULL;
            }
          task->inbound[task->samples] =
              micros(ack.received_cycles - ack.sent_cycles);
          task->outbound[task->samples] =
              micros(collected_cycles - ack.received_cycles);
          task->samples++;
        }
#ifdef NXRS_CQ_PROFILE_PHASES
      task->validate_cycles += cycles() - phase_started;
#endif
    }
  for (unsigned lane = 0; lane < lanes(&scenario->config); lane++)
    {
      for (unsigned producer = 0; producer < PRODUCERS; producer++)
        {
          if (next[lane][producer] != scenario->config.sequences)
            {
              task->error = "collector missed an outbound event";
              return NULL;
            }
      }
    }
  if (task->received != events(&scenario->config) ||
      task->samples != samples(&scenario->config))
    {
      task->error = "collector event or sample count mismatch";
      return NULL;
    }
#ifdef NXRS_CQ_SYNCHRONIZED
  nxrs_cq_gate_done();
#endif
  task->stack_hwm = stack_hwm();
  return NULL;
}

static int compare_u32(const void *left, const void *right)
{
  uint32_t a = *(const uint32_t *)left;
  uint32_t b = *(const uint32_t *)right;
  return (a > b) - (a < b);
}

static uint32_t percentile(uint32_t *samples, unsigned count, unsigned percent)
{
  qsort(samples, count, sizeof(*samples), compare_u32);
  unsigned rank = (count * percent + 99u) / 100u;
  return samples[rank ? rank - 1u : 0u];
}

static int run_scenario(struct config config, unsigned seed)
{
  uint32_t heap_before = 0;
#ifdef NXRS_CQ_DEVICE
  heap_before = nxrs_cq_heap_used();
#endif
  struct scenario *scenario = calloc(1, sizeof(*scenario));
  if (!scenario)
    {
      return -1;
    }
  scenario->config = config;
  pthread_t producers[PRODUCERS];
  pthread_t workers[MAX_WORKERS];
  pthread_t collector;
  unsigned created = 0;
  for (unsigned lane = 0; lane < input_queues(&config); lane++)
    {
#ifdef NXRS_CQ_MULTIPLEXED
      long capacity = config.lanes_per_worker;
#else
      long capacity = 1;
#endif
      if (create_queue(&scenario->inputs[lane], seed + lane, capacity,
                       sizeof(struct event)) != 0)
        {
          goto cleanup;
        }
      created++;
    }
  for (unsigned i = 0; i < config.workers; i++)
    {
      if (create_queue(&scenario->outputs[i], seed + input_queues(&config) + i,
                       4, sizeof(struct ack)) != 0)
        {
          goto cleanup;
        }
      created++;
    }
  uint32_t heap_queues = 0;
#ifdef NXRS_CQ_DEVICE
  heap_queues = nxrs_cq_heap_used();
#endif
#ifdef NXRS_CQ_SYNCHRONIZED
  if (nxrs_cq_gate_init(config.workers + PRODUCERS + 1u) != 0)
    {
      goto cleanup;
    }
#endif
  scenario->collector.scenario = scenario;
  if (spawn(&collector, collector_main, &scenario->collector,
            COLLECTOR_STACK) != 0)
    {
      goto cleanup;
    }
  for (unsigned i = 0; i < config.workers; i++)
    {
      scenario->workers[i].scenario = scenario;
      scenario->workers[i].id = i;
      if (spawn(&workers[i], worker_main, &scenario->workers[i],
                WORKER_STACK) != 0)
        {
          fprintf(stderr, "C_SCALE_FAIL worker spawn\n");
          return -1;
        }
    }
  uint32_t heap_workers = 0;
#ifdef NXRS_CQ_DEVICE
  heap_workers = nxrs_cq_heap_used();
#endif
  uint64_t started = monotonic_us();
  for (unsigned i = 0; i < PRODUCERS; i++)
    {
      scenario->producers[i].scenario = scenario;
      scenario->producers[i].id = i;
      if (spawn(&producers[i], producer_main, &scenario->producers[i],
                WORKER_STACK) != 0)
        {
          fprintf(stderr, "C_SCALE_FAIL producer spawn\n");
          return -1;
        }
    }
  uint32_t heap_producers = 0;
#ifdef NXRS_CQ_DEVICE
  heap_producers = nxrs_cq_heap_used();
#endif
#ifdef NXRS_CQ_SYNCHRONIZED
  if (nxrs_cq_gate_release() != 0)
    {
      fprintf(stderr, "C_SCALE_FAIL transport gate release\n");
      return -1;
    }
  heap_producers = nxrs_cq_gate_heap_ready();
#endif
  for (unsigned i = 0; i < PRODUCERS; i++)
    {
      pthread_join(producers[i], NULL);
    }
  for (unsigned i = 0; i < config.workers; i++)
    {
      pthread_join(workers[i], NULL);
    }
  pthread_join(collector, NULL);
  uint32_t heap_after_join = 0;
#ifdef NXRS_CQ_DEVICE
  heap_after_join = nxrs_cq_heap_used();
#endif
  uint64_t elapsed_us = monotonic_us() - started;
#ifdef NXRS_CQ_SYNCHRONIZED
  uint32_t transport_cycles = nxrs_cq_gate_elapsed();
  if (nxrs_cq_gate_destroy() != 0)
    {
      fprintf(stderr, "C_SCALE_FAIL transport gate destroy\n");
      goto cleanup;
    }
#endif
  for (unsigned i = 0; i < PRODUCERS; i++)
    {
      if (scenario->producers[i].error)
        {
          fprintf(stderr, "C_SCALE_FAIL %s\n", scenario->producers[i].error);
          goto cleanup;
        }
    }
  for (unsigned i = 0; i < config.workers; i++)
    {
      if (scenario->workers[i].error)
        {
          fprintf(stderr, "C_SCALE_FAIL %s\n", scenario->workers[i].error);
          goto cleanup;
        }
    }
  if (scenario->collector.error || scenario->collector.received != events(&config) ||
      scenario->collector.samples != samples(&config))
    {
      fprintf(stderr, "C_SCALE_FAIL %s\n",
              scenario->collector.error ? scenario->collector.error : "event count");
      goto cleanup;
    }
#ifdef NXRS_CQ_SYNCHRONIZED
  if (strcmp(config.label, "large") == 0)
    {
      printf("CQ_TRANSPORT_PASS language=c mode=large cycles=%lu "
             "event_bytes=%lu ack_bytes=%lu\n",
             (unsigned long)transport_cycles,
             (unsigned long)sizeof(struct event),
             (unsigned long)sizeof(struct ack));
    }
#endif
  uint32_t max_inbound = 0;
  for (unsigned i = 0; i < scenario->collector.samples; i++)
    {
      if (scenario->collector.inbound[i] > max_inbound)
        {
          max_inbound = scenario->collector.inbound[i];
        }
    }
  uint32_t outbound_p99 = percentile(scenario->collector.outbound,
                                      scenario->collector.samples, 99);
  uint32_t inbound_p50 = percentile(scenario->collector.inbound,
                                     scenario->collector.samples, 50);
  uint32_t inbound_p99 = percentile(scenario->collector.inbound,
                                     scenario->collector.samples, 99);
  uint32_t producer_stack_max = 0;
  uint32_t worker_stack_max = 0;
  for (unsigned i = 0; i < PRODUCERS; i++)
    {
      if (scenario->producers[i].stack_hwm > producer_stack_max)
        {
          producer_stack_max = scenario->producers[i].stack_hwm;
        }
    }
  for (unsigned i = 0; i < config.workers; i++)
    {
      if (scenario->workers[i].stack_hwm > worker_stack_max)
        {
          worker_stack_max = scenario->workers[i].stack_hwm;
        }
    }
  printf("CQ_C_SCALE_PASS mode=%s queues=%u logical_streams=%u "
         "threads=%u messages=%u "
         "elapsed_us=%llu rx_p50_us=%lu rx_p99_us=%lu rx_max_us=%lu "
         "ack_p99_us=%lu digest=%lu stack_main=%lu "
         "stack_producer_max=%lu stack_worker_max=%lu stack_collector=%lu "
         "heap_before=%lu heap_queues=%lu heap_workers=%lu "
         "heap_producers=%lu "
         "heap_after_join=%lu\n",
         config.label, input_queues(&config) + config.workers
         , lanes(&config) + config.workers
         , config.workers + PRODUCERS + 1
         , events(&config),
         (unsigned long long)elapsed_us, (unsigned long)inbound_p50,
         (unsigned long)inbound_p99, (unsigned long)max_inbound,
         (unsigned long)outbound_p99,
         (unsigned long)scenario->collector.digest,
         (unsigned long)stack_hwm(), (unsigned long)producer_stack_max,
         (unsigned long)worker_stack_max,
         (unsigned long)scenario->collector.stack_hwm,
         (unsigned long)heap_before, (unsigned long)heap_queues,
         (unsigned long)heap_workers, (unsigned long)heap_producers,
         (unsigned long)heap_after_join);
#ifdef NXRS_CQ_PROFILE_PHASES
  uint64_t producer_build = 0;
  uint64_t producer_send = 0;
  uint64_t worker_poll = 0;
  uint64_t worker_receive = 0;
  uint64_t worker_validate = 0;
  uint64_t worker_send = 0;
  for (unsigned i = 0; i < PRODUCERS; i++)
    {
      producer_build += scenario->producers[i].build_cycles;
      producer_send += scenario->producers[i].send_cycles;
    }
  for (unsigned i = 0; i < config.workers; i++)
    {
      worker_poll += scenario->workers[i].poll_cycles;
      worker_receive += scenario->workers[i].receive_cycles;
      worker_validate += scenario->workers[i].validate_cycles;
      worker_send += scenario->workers[i].send_cycles;
    }
  printf("CQ_C_PROFILE_PASS mode=%s producer_build_us=%llu "
         "producer_send_us=%llu worker_poll_us=%llu "
         "worker_receive_us=%llu worker_validate_us=%llu "
         "worker_send_us=%llu collector_poll_us=%llu "
         "collector_receive_us=%llu collector_validate_us=%llu\n",
         config.label,
         (unsigned long long)(producer_build / 240u),
         (unsigned long long)(producer_send / 240u),
         (unsigned long long)(worker_poll / 240u),
         (unsigned long long)(worker_receive / 240u),
         (unsigned long long)(worker_validate / 240u),
         (unsigned long long)(worker_send / 240u),
         (unsigned long long)(scenario->collector.poll_cycles / 240u),
         (unsigned long long)(scenario->collector.receive_cycles / 240u),
         (unsigned long long)(scenario->collector.validate_cycles / 240u));
#endif
  for (unsigned i = 0; i < created; i++)
    {
      close_queue(i < input_queues(&config) ? &scenario->inputs[i] :
                  &scenario->outputs[i - input_queues(&config)]);
    }
  free(scenario);
  return 0;

cleanup:
  for (unsigned i = 0; i < created; i++)
    {
      close_queue(i < input_queues(&config) ? &scenario->inputs[i] :
                  &scenario->outputs[i - input_queues(&config)]);
    }
  free(scenario);
  return -1;
}

struct wake_probe
{
  uint32_t sent_cycles;
};

struct wake_ack
{
  uint32_t sent_cycles;
  uint32_t received_cycles;
};

struct wake_task
{
  struct queue inputs[2];
  struct queue output;
  const char *error;
};

static void *wake_main(void *argument)
{
  struct wake_task *task = argument;
  struct pollfd fds[2] = {{.fd = task->inputs[0].fd, .events = POLLIN},
                          {.fd = task->inputs[1].fd, .events = POLLIN}};
  unsigned cursor = 0;
  for (unsigned i = 0; i < WAKE_WARMUP + WAKE_TRIALS; i++)
    {
      int which = ready_index(fds, 2, &cursor);
      struct wake_probe probe;
      if (which < 0 || mq_receive(fds[which].fd, (char *)&probe,
                                  sizeof(probe), NULL) != sizeof(probe))
        {
          task->error = "wake poll or receive failed";
          return NULL;
        }
      struct wake_ack ack = {.sent_cycles = probe.sent_cycles,
                             .received_cycles = cycles()};
      if (mq_send(task->output.fd, (const char *)&ack, sizeof(ack), 0) != 0)
        {
          task->error = "wake ack send failed";
          return NULL;
        }
    }
  return NULL;
}

static int run_wake(void)
{
  struct wake_task task = {0};
  pthread_t worker;
  unsigned created = 0;
  for (unsigned i = 0; i < 2; i++)
    {
      if (create_queue(&task.inputs[i], 60 + i, 1,
                       sizeof(struct wake_probe)) != 0)
        {
          goto cleanup;
        }
      created++;
    }
  if (create_queue(&task.output, 62, 1, sizeof(struct wake_ack)) != 0)
    {
      goto cleanup;
    }
  created++;
  if (spawn(&worker, wake_main, &task, WORKER_STACK) != 0)
    {
      goto cleanup;
    }
  uint32_t inbound[WAKE_TRIALS];
  uint32_t outbound[WAKE_TRIALS];
  uint32_t sleep_clock_us = 0;
  for (unsigned i = 0; i < WAKE_WARMUP + WAKE_TRIALS; i++)
    {
      uint32_t sleep_started = cycles();
      sleep_ms(20);
      if (i == 0)
        {
          sleep_clock_us = micros(cycles() - sleep_started);
        }
      struct wake_probe probe = {.sent_cycles = cycles()};
      if (mq_send(task.inputs[i % 2].fd, (const char *)&probe,
                  sizeof(probe), 0) != 0)
        {
          goto cleanup;
        }
      struct wake_ack ack;
      if (mq_receive(task.output.fd, (char *)&ack,
                     sizeof(ack), NULL) != sizeof(ack) ||
          ack.sent_cycles != probe.sent_cycles)
        {
          goto cleanup;
        }
      uint32_t completed = cycles();
      if (i >= WAKE_WARMUP)
        {
          inbound[i - WAKE_WARMUP] =
              micros(ack.received_cycles - ack.sent_cycles);
          outbound[i - WAKE_WARMUP] =
              micros(completed - ack.received_cycles);
        }
    }
  pthread_join(worker, NULL);
  if (task.error)
    {
      fprintf(stderr, "C_WAKE_FAIL %s\n", task.error);
      goto cleanup;
    }
  uint32_t max_inbound = 0;
  for (unsigned i = 0; i < WAKE_TRIALS; i++)
    {
      if (inbound[i] > max_inbound)
        {
          max_inbound = inbound[i];
        }
    }
  uint32_t outbound_p99 = percentile(outbound, WAKE_TRIALS, 99);
  uint32_t inbound_p50 = percentile(inbound, WAKE_TRIALS, 50);
  uint32_t inbound_p99 = percentile(inbound, WAKE_TRIALS, 99);
  printf("CQ_C_WAKE_PASS trials=%u queues=3 threads=2 sleep_clock_us=%lu "
         "rx_p50_us=%lu rx_p99_us=%lu rx_max_us=%lu ack_p99_us=%lu\n",
         WAKE_TRIALS, (unsigned long)sleep_clock_us,
         (unsigned long)inbound_p50, (unsigned long)inbound_p99,
         (unsigned long)max_inbound, (unsigned long)outbound_p99);
  for (unsigned i = 0; i < created; i++)
    {
      close_queue(i < 2 ? &task.inputs[i] : &task.output);
    }
  return 0;

cleanup:
  for (unsigned i = 0; i < created; i++)
    {
      close_queue(i < 2 ? &task.inputs[i] : &task.output);
    }
  return -1;
}

#if defined(NXRS_CQ_FLASH_THREAD_ONLY) && defined(NXRS_CQ_FLASH_MQ_PROBE)
#  error "flash thread and mqueue rungs are mutually exclusive"
#endif

#ifdef NXRS_CQ_FLASH_MQ_PROBE
static int run_flash_mq_probe(uint32_t *checksum)
{
  struct queue queue = {.fd = -1};
  struct ack sent = {.lane = 1, .producer = 2, .sequence = 3,
                     .checksum = 0xa5a5a5a5u, .sent_cycles = 4,
                     .received_cycles = 5};
  struct ack received;
  if (create_queue(&queue, 97, 1, sizeof(sent)) != 0)
    {
      return -1;
    }
  int status = -1;
  struct pollfd ready = {.fd = queue.fd, .events = POLLIN};
  if (mq_send(queue.fd, (const char *)&sent, sizeof(sent), 0) == 0 &&
      poll(&ready, 1, -1) == 1 &&
      mq_receive(queue.fd, (char *)&received, sizeof(received), NULL) ==
          sizeof(received) &&
      ready.revents == POLLIN &&
      received.lane == sent.lane &&
      received.producer == sent.producer &&
      received.sequence == sent.sequence &&
      received.checksum == sent.checksum &&
      received.sent_cycles == sent.sent_cycles &&
      received.received_cycles == sent.received_cycles)
    {
      *checksum = received.checksum;
      status = 0;
    }
  close_queue(&queue);
  return status;
}

#ifdef NXRS_CQ_FLASH_REPORT_PROBE
static void print_flash_report(uint32_t checksum)
{
  uint32_t samples[64];
  uint32_t seed = cycles();
  for (unsigned i = 0; i < 64; i++)
    {
      samples[i] = seed ^ (63 - i);
    }
  uint32_t p99 = percentile(samples, 64, 99);
  printf("CQ_C_FLASH_REPORT_PASS checksum=%lu p99=%lu samples=64\n",
         (unsigned long)checksum, (unsigned long)p99);
}
#endif
#endif

#ifdef NXRS_CQ_EMBED_CONTROL
int nxrs_c_scale_control_main(int argc, char **argv)
#else
int main(int argc, char **argv)
#endif
{
#if   defined(NXRS_CQ_FLASH_THREAD_ONLY)
  (void)argc;
  (void)argv;
  return run_thread_baseline() == 0 ? 0 : 1;
#elif defined(NXRS_CQ_FLASH_MQ_PROBE)
  (void)argc;
  (void)argv;
  if (run_thread_baseline() != 0)
    {
      return 1;
    }
  uint32_t checksum;
  if (run_flash_mq_probe(&checksum) != 0)
    {
      fprintf(stderr, "CQ_C_FLASH_MQ_FAIL\n");
      return 1;
    }
  puts("CQ_C_FLASH_MQ_PASS");
#ifdef NXRS_CQ_FLASH_REPORT_PROBE
  print_flash_report(checksum);
#else
  (void)checksum;
#endif
  return 0;
#else
  if (argc == 2 && strcmp(argv[1], "threads") == 0)
    {
      return run_thread_baseline() == 0 ? 0 : 1;
    }
  if (argc > 2 || (argc == 2 && strcmp(argv[1], "large") != 0))
    {
      fprintf(stderr, "CQ_C_SCALE_FAIL unknown mode\n");
      return 1;
    }
  bool large_only = argc == 2;
  const struct config configurations[] = {
      {.label = "small", .workers = 1, .lanes_per_worker = 2,
       .sequences = 64},
      {.label = "medium", .workers = 12, .lanes_per_worker = 2,
       .sequences = 32},
#ifdef NXRS_CQ_COMPACT_TOPOLOGY
      {.label = "large", .workers = 5, .lanes_per_worker = 11,
       .sequences = 26},
#else
      {.label = "large", .workers = 15, .lanes_per_worker = 3,
       .sequences = 32},
#endif
  };
  for (unsigned i = 0; i < sizeof(configurations) / sizeof(configurations[0]); i++)
    {
      if (large_only && i != 2)
        {
          continue;
        }
      if (run_scenario(configurations[i], i * MAX_QUEUES) != 0)
        {
          fprintf(stderr, "CQ_C_SCALE_FAIL mode=%s\n", configurations[i].label);
          return 1;
        }
    }
#ifndef NXRS_CQ_EMBED_CONTROL
  if (run_wake() != 0)
    {
      fprintf(stderr, "CQ_C_WAKE_FAIL\n");
      return 1;
    }
#endif
  return 0;
#endif
}
