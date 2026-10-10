/* SPDX-License-Identifier: MIT */
#include "native_adapter.h"
#include "memory.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/sys/atomic.h>

BUILD_ASSERT(CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC == 240000000);
BUILD_ASSERT(!IS_ENABLED(CONFIG_SMP));
#define INPUTS 45
#define OUTPUTS 15
#define THREADS 20
#define PRIORITY 5

static struct k_msgq queues[INPUTS + OUTPUTS];
static bool opened[INPUTS + OUTPUTS];
static char input_buffers[INPUTS][248];
static char output_buffers[OUTPUTS][4 * NXRS_ACK_BYTES];
static struct k_thread threads[THREADS];
K_THREAD_STACK_DEFINE(collector_stack, 6144);
K_THREAD_STACK_ARRAY_DEFINE(role_stacks, THREADS - 1, 4096);
static struct { void *(*entry)(void *); void *argument; void *result; } entries[THREADS];
static unsigned next_thread;
static struct k_sem ready, go;
static unsigned participants;
static uint32_t start_cycles, elapsed_cycles;
static int64_t wall_started, wall_done;
static bool gate_initialized;

void nxrs_adapter_reset(void)
{
  /* Only after successful joins; no cancellation or concurrent commands. */
  __ASSERT_NO_MSG(next_thread == 0 || next_thread == THREADS);
  for (unsigned i = 0; i < INPUTS + OUTPUTS; ++i) __ASSERT_NO_MSG(!opened[i]);
  next_thread = 0;
}

static int error(int value) { errno = value; return -1; }
void nxrs_perror(const char *name) { printf("%s: error=%d\n", name, errno); }

int nxrs_mq_open(const char *name, int flags, unsigned mode, const struct mq_attr *attr)
{
  (void)name; (void)flags; (void)mode;
  if (!attr) return error(EINVAL);
  unsigned first, end;
  char *buffer;
  if (attr->mq_msgsize == 248 && attr->mq_maxmsg == 1) { first = 0; end = INPUTS; }
  else if (attr->mq_msgsize == NXRS_ACK_BYTES && attr->mq_maxmsg == 4) {
    first = INPUTS; end = INPUTS + OUTPUTS;
  } else return error(EINVAL);
  for (unsigned i = first; i < end; ++i) {
    if (opened[i]) continue;
    buffer = i < INPUTS ? input_buffers[i] : output_buffers[i - INPUTS];
    k_msgq_init(&queues[i], buffer, attr->mq_msgsize, attr->mq_maxmsg);
    opened[i] = true;
    return (int)i;
  }
  return error(ENOSPC);
}

static bool valid(int fd) { return fd >= 0 && fd < INPUTS + OUTPUTS && opened[fd]; }
int nxrs_mq_close(int fd)
{
  if (!valid(fd) || k_msgq_num_used_get(&queues[fd])) return error(EINVAL);
  opened[fd] = false;
  return 0;
}
int nxrs_mq_send(int fd, const char *data, size_t bytes, unsigned priority)
{
  if (!valid(fd) || bytes != queues[fd].msg_size || priority != 0) return error(EINVAL);
  int rc = k_msgq_put(&queues[fd], data, K_FOREVER);
  return rc == 0 ? 0 : error(ETIMEDOUT);
}
long nxrs_mq_receive(int fd, char *data, size_t bytes, unsigned *priority)
{
  if (!valid(fd) || bytes < queues[fd].msg_size) return error(EINVAL);
  /* Each queue has one receiving owner; it has just observed readiness. */
  int rc = k_msgq_get(&queues[fd], data, K_NO_WAIT);
  if (rc) return error(ETIMEDOUT);
  if (priority) *priority = 0;
  return queues[fd].msg_size;
}
int nxrs_poll(struct pollfd *fds, unsigned count, int timeout)
{
  if (!fds || count == 0 || count > OUTPUTS || timeout != -1) return error(EINVAL);
  struct k_poll_event events[OUTPUTS];
  for (unsigned i = 0; i < count; ++i) {
    if (!valid(fds[i].fd) || fds[i].events != POLLIN) return error(EINVAL);
    fds[i].revents = 0;
    k_poll_event_init(&events[i], K_POLL_TYPE_MSGQ_DATA_AVAILABLE,
                     K_POLL_MODE_NOTIFY_ONLY, &queues[fds[i].fd]);
  }
  if (k_poll(events, count, K_FOREVER)) return error(EIO);
  int ready_count = 0;
  for (unsigned i = 0; i < count; ++i) {
    if (events[i].state == K_POLL_STATE_MSGQ_DATA_AVAILABLE) {
      fds[i].revents = POLLIN;
      ++ready_count;
    }
  }
  return ready_count;
}
static void thread_entry(void *index, void *unused1, void *unused2)
{
  (void)unused1; (void)unused2;
  unsigned i = (unsigned)(uintptr_t)index;
  entries[i].result = entries[i].entry(entries[i].argument);
}
int nxrs_thread_create(nxrs_thread_id *id, const nxrs_thread_attr *attr,
                       void *(*entry)(void *), void *argument)
{
  unsigned i = next_thread;
  if (!id || !attr || !entry || i >= THREADS ||
      attr->stack_size != (i == 0 ? 6144 : 4096)) return EINVAL;
  entries[i].entry = entry; entries[i].argument = argument; entries[i].result = NULL;
  k_thread_stack_t *stack = i == 0 ? collector_stack : role_stacks[i - 1];
  size_t size = i == 0 ? K_THREAD_STACK_SIZEOF(collector_stack) :
                         K_THREAD_STACK_SIZEOF(role_stacks[0]);
  k_thread_create(&threads[i], stack, size, thread_entry, (void *)(uintptr_t)i,
                   NULL, NULL, PRIORITY, 0, K_NO_WAIT);
  *id = next_thread++;
  return 0;
}
int nxrs_thread_join(nxrs_thread_id id, void **result)
{
  if (id >= next_thread) return EINVAL;
  int rc = k_thread_join(&threads[id], K_SECONDS(15));
  if (result && !rc) *result = entries[id].result;
  return rc;
}
int nxrs_clock_gettime(int clock, struct timespec *out)
{
  (void)clock;
  int64_t ns = k_ticks_to_ns_floor64(k_uptime_ticks());
  out->tv_sec = ns / INT64_C(1000000000); out->tv_nsec = ns % INT64_C(1000000000);
  return 0;
}
int nxrs_nanosleep(const struct timespec *delay, struct timespec *remaining)
{
  (void)remaining;
  k_sleep(K_NSEC((int64_t)delay->tv_sec * INT64_C(1000000000) + delay->tv_nsec));
  return 0;
}
uint32_t nxrs_cq_cycles(void)
{
  uint32_t cycles;
  __asm__ volatile("rsr.ccount %0" : "=a"(cycles));
  return cycles;
}
uint32_t nxrs_cq_stack_hwm(void)
{
  size_t unused;
  struct k_thread *thread = k_current_get();
  if (k_thread_stack_space_get(thread, &unused)) return 0;
  return thread->stack_info.size - unused;
}
int nxrs_cq_gate_init(unsigned count)
{
  if (gate_initialized || count != THREADS) return -1;
  k_sem_init(&ready, 0, THREADS); k_sem_init(&go, 0, THREADS);
  participants = count; elapsed_cycles = 0; gate_initialized = true;
  return 0;
}
int nxrs_cq_gate_arrive(void)
{
  if (!gate_initialized) return -1;
  k_sem_give(&ready);
  return k_sem_take(&go, K_SECONDS(10));
}
int nxrs_cq_gate_release(void)
{
  for (unsigned i = 0; i < participants; ++i)
    if (k_sem_take(&ready, K_SECONDS(10))) return -1;
  wall_started = k_uptime_ticks(); start_cycles = nxrs_cq_cycles();
  for (unsigned i = 0; i < participants; ++i) k_sem_give(&go);
  return 0;
}
void nxrs_cq_gate_done(void)
{
  elapsed_cycles = nxrs_cq_cycles() - start_cycles; wall_done = k_uptime_ticks();
}
uint32_t nxrs_cq_gate_elapsed(void)
{
  return k_ticks_to_ms_floor64(wall_done - wall_started) < 16000 ? elapsed_cycles : 0;
}
uint32_t nxrs_cq_gate_heap_ready(void) { return nxrs_cq_heap_used(); }
int nxrs_cq_gate_destroy(void) { gate_initialized = false; return 0; }
void nxrs_adapter_report(void)
{
  printf("ZEPHYR_RESOURCES queue_buffers=%u queue_objects=%u thread_objects=%u "
         "stack_storage=%u entry_storage=%u heap_allocated=0\n",
         (unsigned)(sizeof(input_buffers) + sizeof(output_buffers)),
         (unsigned)(sizeof(queues) + sizeof(opened)), (unsigned)sizeof(threads),
         (unsigned)(sizeof(collector_stack) + sizeof(role_stacks)), (unsigned)sizeof(entries));
}
