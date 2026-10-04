/* Qualification-only common timing boundary. Setup and thread creation are
 * excluded; release-to-validated-last-reply includes scheduling and traffic.
 * One command owns this gate, and destroys it only after every thread joins.
 */
#define _POSIX_C_SOURCE 200809L
#include <pthread.h>
#include <stdint.h>
#include <time.h>

extern uint32_t nxrs_cq_cycles(void);
extern uint32_t nxrs_cq_heap_used(void);
static pthread_mutex_t lock;
static pthread_cond_t ready, go;
static unsigned participants, arrived;
static int released;
static uint32_t started, elapsed, heap_ready;
static struct timespec wall_started, wall_done;
static int initialized;

int nxrs_cq_gate_init(unsigned count)
{
  if (initialized || count == 0 || count > 64) return -1;
  if (pthread_mutex_init(&lock, NULL) != 0) return -1;
  if (pthread_cond_init(&ready, NULL) != 0) { pthread_mutex_destroy(&lock); return -1; }
  if (pthread_cond_init(&go, NULL) != 0) {
    pthread_cond_destroy(&ready); pthread_mutex_destroy(&lock); return -1;
  }
  participants = count; arrived = 0; released = 0; elapsed = 0; initialized = 1;
  return 0;
}

int nxrs_cq_gate_arrive(void)
{
  if (!initialized || pthread_mutex_lock(&lock) != 0) return -1;
  ++arrived;
  pthread_cond_signal(&ready);
  int result = 0;
  while (!released && result == 0) result = pthread_cond_wait(&go, &lock);
  pthread_mutex_unlock(&lock);
  return result == 0 ? 0 : -1;
}

int nxrs_cq_gate_release(void)
{
  if (!initialized || pthread_mutex_lock(&lock) != 0) return -1;
  while (arrived != participants)
    if (pthread_cond_wait(&ready, &lock) != 0) { pthread_mutex_unlock(&lock); return -1; }
  heap_ready = nxrs_cq_heap_used();
  if (clock_gettime(CLOCK_MONOTONIC, &wall_started) != 0) { pthread_mutex_unlock(&lock); return -1; }
  started = nxrs_cq_cycles();
  released = 1;
  int result = pthread_cond_broadcast(&go);
  pthread_mutex_unlock(&lock);
  return result == 0 ? 0 : -1;
}

void nxrs_cq_gate_done(void)
{
  elapsed = nxrs_cq_cycles() - started;
  clock_gettime(CLOCK_MONOTONIC, &wall_done);
}

uint32_t nxrs_cq_gate_elapsed(void)
{
  /* CCOUNT wraps after about 17.9 seconds at 240 MHz. Reject long samples
   * instead of silently reporting a modulo duration as the full runtime. */
  int64_t ns = (int64_t)(wall_done.tv_sec - wall_started.tv_sec) * 1000000000 +
               wall_done.tv_nsec - wall_started.tv_nsec;
  return ns > 0 && ns < INT64_C(16000000000) ? elapsed : 0;
}

uint32_t nxrs_cq_gate_heap_ready(void) { return heap_ready; }

int nxrs_cq_gate_destroy(void)
{
  if (!initialized) return -1;
  int a = pthread_cond_destroy(&ready), b = pthread_cond_destroy(&go);
  int c = pthread_mutex_destroy(&lock);
  initialized = 0;
  return a == 0 && b == 0 && c == 0 ? 0 : -1;
}
