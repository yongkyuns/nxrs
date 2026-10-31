/* Actual NuttX lifecycle, poll loop and mutex code with host kernel primitives. */
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FAR
#define OK 0
#define DEBUGASSERT assert
#define CONFIG_SMP_NCPUS 3
#define CONFIG_DEFAULT_TASK_STACKSIZE 2048
#define CONFIG_IOB_NCHAINS 0
#define INVALID_PROCESS_ID (-1)
#define NETDEV_THREAD_NAME_FMT "netdev-%s"
#define IFF_UP 1
#define IFF_RUNNING 2
#define IFF_IS_UP(flags) ((flags) & IFF_UP)
#define nerr(...) ((void)0)
#define nwarn(...) ((void)0)
#define NETDEV_RXDROPPED(dev) ((void)0)
#define NETDEV_RXPACKETS(dev) ((void)0)
#define NETDEV_DOWN 0
#define TASK_CANCEL_ENABLE 0
#define TASK_CANCEL_DISABLE 1
#ifdef CPU_ZERO
#undef CPU_ZERO
#undef CPU_SET
#endif
#ifdef __APPLE__
typedef int cpu_set_t;
#endif
#define CPU_ZERO(set) ((void)(set))
#define CPU_SET(cpu, set) ((void)(cpu), (void)(set))
#define sched_setaffinity(tid, size, set) ((void)(tid), (void)(size), (void)(set))
typedef _Atomic int atomic_t;
#define atomic_xchg_acquire(ptr, value) atomic_exchange_explicit(ptr, value, memory_order_acquire)
#define atomic_read_acquire(ptr) atomic_load_explicit(ptr, memory_order_acquire)
#define atomic_set(ptr, value) atomic_store_explicit(ptr, value, memory_order_relaxed)
#define atomic_set_release(ptr, value) atomic_store_explicit(ptr, value, memory_order_release)
typedef struct { pthread_mutex_t lock; pthread_cond_t changed; unsigned count; } sem_t;
typedef struct { pthread_mutex_t lock; _Atomic uintptr_t owner; } mutex_t;
typedef struct { mutex_t mutex; unsigned count; } rmutex_t;
struct work_s { int unused; };
typedef int netpkt_t;
enum { NETDEV_RX_WORK, NETDEV_RX_DIRECT, NETDEV_RX_THREAD, NETDEV_RX_THREAD_RSS };
enum { NETPKT_RX };
struct netdev_lowerhalf_s;
struct net_driver_s {
  void *d_private; rmutex_t d_lock; unsigned d_flags; int d_lltype; char d_ifname[16];
  int (*d_ifup)(struct net_driver_s *);
  int (*d_ifdown)(struct net_driver_s *);
};
struct netdev_ops_s {
  int (*ifup)(struct netdev_lowerhalf_s *);
  int (*ifdown)(struct netdev_lowerhalf_s *);
  netpkt_t *(*receive)(struct netdev_lowerhalf_s *);
};
struct netdev_lowerhalf_s {
  const struct netdev_ops_s *ops;
  unsigned rxtype, priority;
  struct net_driver_s netdev;
};
struct netdev_upperhalf_s;
static int nxmutex_lock(mutex_t *);
static int nxmutex_unlock(mutex_t *);
int task_setcancelstate(int, int *);
static bool nxrmutex_is_hold(rmutex_t *);
static void netdev_lock(struct net_driver_s *);
static void netdev_unlock(struct net_driver_s *);
static int nxsem_wait(sem_t *);
int nxsem_wait_uninterruptible(sem_t *);
static int nxsem_post(sem_t *);
static void nxsem_destroy(sem_t *);
static int kthread_create(const char *, int, int, int (*)(int, char **), char **);
static int netdev_unregister(struct net_driver_s *);
static void kmm_free(void *);
static void netdev_upper_txavail_work(struct netdev_upperhalf_s *);
static void work_cancel_sync(unsigned p, struct work_s *w) { (void)p; (void)w; }
static void netpkt_free(struct netdev_lowerhalf_s *d, netpkt_t *p, int t) { (void)d; (void)p; (void)t; }
static void netpkt_put(struct net_driver_s *d, netpkt_t *p, int t) { (void)d; (void)p; (void)t; }
static void netlink_device_notify(struct net_driver_s *d) { (void)d; }
static void devif_dev_event(struct net_driver_s *d, int e) { (void)d; (void)e; }
static rmutex_t g_netlock;
#include "locks.inc"
#include "kernel.inc"

static struct netdev_lowerhalf_s device;
static struct netdev_upperhalf_s *current;
static _Atomic bool alive[CONFIG_SMP_NCPUS];
static unsigned created, lower_stops, unregistered, freed, work_done;
static unsigned cancel_waits, fail_create, exit_waits;
static int start_error;
static int restore_error;
static mutex_t *fail_restore;
static bool real_worker, early_worker, admission_probe;
static _Thread_local bool poll_thread;
static _Thread_local int cancelstate;
static pthread_t worker;
static pthread_mutex_t control = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static bool launch, waiting_dev, waiting_exit;

static void mutex_init(mutex_t *m)
{ assert(pthread_mutex_init(&m->lock, NULL) == 0); atomic_store(&m->owner, 0); }
static bool nxrmutex_is_hold(rmutex_t *m)
{ return atomic_load(&m->mutex.owner) == (uintptr_t)pthread_self(); }
static int nxmutex_lock(mutex_t *m)
{
  if (m == fail_restore) {
    assert(cancelstate == TASK_CANCEL_DISABLE);
    fail_restore = NULL; return restore_error;
  }
  assert(pthread_mutex_lock(&m->lock) == 0);
  atomic_store(&m->owner, (uintptr_t)pthread_self()); return 0;
}
int task_setcancelstate(int state, int *old)
{
  assert(state == TASK_CANCEL_ENABLE || state == TASK_CANCEL_DISABLE);
  if (old) *old = cancelstate;
  cancelstate = state; return 0;
}
static int nxmutex_unlock(mutex_t *m)
{
  assert(atomic_load(&m->owner) == (uintptr_t)pthread_self());
  atomic_store(&m->owner, 0); assert(pthread_mutex_unlock(&m->lock) == 0); return 0;
}
static void netdev_lock(struct net_driver_s *d)
{
  if (poll_thread) {
    pthread_mutex_lock(&control); waiting_dev = true;
    pthread_cond_broadcast(&changed); pthread_mutex_unlock(&control);
  }
  assert(nxrmutex_lock(&d->d_lock) == 0);
}
static void netdev_unlock(struct net_driver_s *d)
{ assert(nxrmutex_unlock(&d->d_lock) == 0); }
static void sem_init(sem_t *s)
{
  assert(pthread_mutex_init(&s->lock, NULL) == 0);
  assert(pthread_cond_init(&s->changed, NULL) == 0); s->count = 0;
}
static int exit_cpu(sem_t *s)
{
  for (int i = 0; i < CONFIG_SMP_NCPUS; i++)
    if (current && s == &current->thread[i].sem_exit) return i;
  return -1;
}
static int nxsem_post(sem_t *s)
{
  int cpu = exit_cpu(s);
  if (cpu >= 0) atomic_store(&alive[cpu], false);
  pthread_mutex_lock(&s->lock); s->count++;
  pthread_cond_signal(&s->changed); pthread_mutex_unlock(&s->lock); return 0;
}
static int nxsem_wait(sem_t *s)
{
  int cpu = exit_cpu(s);
  if (cpu >= 0) {
    exit_waits++;
    if (cancel_waits) { cancel_waits--; return -ECANCELED; }
    if (admission_probe) {
      admission_probe = false;
      assert(netdev_upper_ifup(&device.netdev) == -EBUSY);
      assert(netdev_upper_ifdown(&device.netdev) == -EBUSY);
      assert(netdev_lower_unregister(&device) == -EBUSY);
    }
    pthread_mutex_lock(&control); waiting_exit = true;
    pthread_cond_broadcast(&changed); pthread_mutex_unlock(&control);
    if (!real_worker) nxsem_post(s); /* Deterministic external thread completion. */
  }
  pthread_mutex_lock(&s->lock);
  while (!s->count) pthread_cond_wait(&s->changed, &s->lock);
  s->count--; pthread_mutex_unlock(&s->lock); return 0;
}
int nxsem_wait_uninterruptible(sem_t *s) { return nxsem_wait(s); }
static void nxsem_destroy(sem_t *s)
{
  assert(!real_worker || !atomic_load(&alive[0]));
  assert(pthread_cond_destroy(&s->changed) == 0);
  assert(pthread_mutex_destroy(&s->lock) == 0);
}
static void *run_worker(void *unused)
{
  (void)unused; char address[32], *args[] = { "netdev", address, "0", NULL };
  snprintf(address, sizeof(address), "%p", (void *)current);
  pthread_mutex_lock(&control);
  while (!launch) pthread_cond_wait(&changed, &control);
  pthread_mutex_unlock(&control); poll_thread = true;
  netdev_upper_loop(3, args); return NULL;
}
static int kthread_create(const char *name, int priority, int stack,
                          int (*entry)(int, char **), char **args)
{
  (void)name; (void)priority; (void)stack;
  assert(entry == netdev_upper_loop);
  unsigned cpu = (unsigned)atoi(args[1]); created++;
  if (created == fail_create) return -EAGAIN;
  assert(!atomic_load(&alive[cpu])); atomic_store(&alive[cpu], true);
  if (real_worker) {
    assert(cpu == 0); assert(pthread_create(&worker, NULL, run_worker, NULL) == 0);
    if (early_worker) {
      nxsem_post(&current->thread[0].sem); /* A late callback left a wake token. */
      pthread_mutex_lock(&control); launch = true; pthread_cond_broadcast(&changed);
      while (!work_done) pthread_cond_wait(&changed, &control);
      pthread_mutex_unlock(&control);
    }
  }
  return 100 + (int)cpu;
}
static int lower_start(struct netdev_lowerhalf_s *d) { (void)d; return start_error; }
static int lower_stop(struct netdev_lowerhalf_s *d)
{
  (void)d;
  for (int i = 0; i < CONFIG_SMP_NCPUS; i++) assert(!atomic_load(&alive[i]));
  lower_stops++; return 0;
}
static netpkt_t *receive(struct netdev_lowerhalf_s *d)
{ (void)d; assert(net_lock() == 0); net_unlock(); return NULL; }
static const struct netdev_ops_s ops = { lower_start, lower_stop, receive };
static void netdev_upper_txavail_work(struct netdev_upperhalf_s *u)
{
  (void)u; pthread_mutex_lock(&control); work_done++;
  pthread_cond_broadcast(&changed); pthread_mutex_unlock(&control);
}
static int netdev_unregister(struct net_driver_s *d)
{
  for (int i = 0; i < CONFIG_SMP_NCPUS; i++) assert(!atomic_load(&alive[i]));
  assert(atomic_load(&d->d_lock.mutex.owner) == 0);
  assert(pthread_mutex_destroy(&d->d_lock.mutex.lock) == 0); unregistered++; return 0;
}
static void kmm_free(void *p)
{
  for (int i = 0; i < CONFIG_SMP_NCPUS; i++) assert(!atomic_load(&alive[i]));
  freed++; free(p); current = NULL;
}
static void setup(unsigned mode)
{
  memset(&device, 0, sizeof(device)); device.ops = &ops; device.rxtype = mode;
  strcpy(device.netdev.d_ifname, "wlan0");
  mutex_init(&device.netdev.d_lock.mutex); mutex_init(&g_netlock.mutex); g_netlock.count = 0;
  current = calloc(1, sizeof(*current) + CONFIG_SMP_NCPUS * sizeof(current->thread[0]));
  assert(current); current->lower = &device; device.netdev.d_private = current;
  device.netdev.d_ifup = netdev_upper_ifup; device.netdev.d_ifdown = netdev_upper_ifdown;
  for (int i = 0; i < CONFIG_SMP_NCPUS; i++) {
    current->thread[i].tid = INVALID_PROCESS_ID;
    if (!i || mode == NETDEV_RX_THREAD_RSS) {
      sem_init(&current->thread[i].sem); sem_init(&current->thread[i].sem_exit);
    }
    atomic_store(&alive[i], false);
  }
  created = lower_stops = unregistered = freed = work_done = exit_waits = 0;
  cancel_waits = fail_create = 0; start_error = 0; fail_restore = NULL;
  restore_error = -ECANCELED;
  real_worker = early_worker = admission_probe = launch = waiting_dev = waiting_exit = false;
}
static void cleanup(void)
{
  assert(cancelstate == TASK_CANCEL_ENABLE);
  assert(netdev_lower_unregister(&device) == 0);
  assert(freed == 1 && unregistered == 1 && device.netdev.d_private == NULL);
  assert(pthread_mutex_destroy(&g_netlock.mutex.lock) == 0);
}
static void cancel_case(void)
{
  setup(NETDEV_RX_THREAD); assert(netdev_ifup(&device.netdev) == 0);
  netdev_lock(&device.netdev); netdev_lock(&device.netdev); cancel_waits = 1;
  fail_restore = &device.netdev.d_lock.mutex; restore_error = -EINTR;
  assert(netdev_ifdown(&device.netdev) == -ECANCELED);
  assert(lower_stops == 0 && atomic_load(&alive[0]) && (device.netdev.d_flags & IFF_UP));
  assert(device.netdev.d_lock.count == 2);
  assert(netdev_ifdown(&device.netdev) == 0 && exit_waits == 2);
  netdev_unlock(&device.netdev); netdev_unlock(&device.netdev); cleanup();

  setup(NETDEV_RX_THREAD); assert(netdev_ifup(&device.netdev) == 0); cancel_waits = 1;
  assert(netdev_lower_unregister(&device) == -ECANCELED);
  assert(!freed && !unregistered && device.netdev.d_private == current);
  cleanup(); /* No caller device/network lock, pending acknowledgement retained. */

  setup(NETDEV_RX_THREAD); assert(netdev_ifup(&device.netdev) == 0);
  assert(net_lock() == 0); assert(net_lock() == 0);
  netdev_lock(&device.netdev); netdev_lock(&device.netdev);
  fail_restore = &g_netlock.mutex;
  assert(netdev_ifdown(&device.netdev) == -ECANCELED);
  assert(!lower_stops && g_netlock.count == 2 && device.netdev.d_lock.count == 2);
  assert(netdev_ifdown(&device.netdev) == 0 && exit_waits == 1);
  netdev_unlock(&device.netdev); netdev_unlock(&device.netdev); net_unlock(); net_unlock(); cleanup();

  setup(NETDEV_RX_THREAD); assert(netdev_ifup(&device.netdev) == 0);
  netdev_lock(&device.netdev); netdev_lock(&device.netdev);
  fail_restore = &device.netdev.d_lock.mutex;
  assert(netdev_ifdown(&device.netdev) == -ECANCELED);
  assert(!lower_stops && device.netdev.d_lock.count == 2);
  assert(netdev_ifdown(&device.netdev) == 0 && exit_waits == 1);
  netdev_unlock(&device.netdev); netdev_unlock(&device.netdev); cleanup();
}
static void startup_case(void)
{
  setup(NETDEV_RX_THREAD); start_error = -EIO;
  assert(netdev_ifup(&device.netdev) == -EIO);
  assert(!atomic_load(&alive[0]) && !(device.netdev.d_flags & IFF_UP)); cleanup();

  setup(NETDEV_RX_THREAD_RSS); fail_create = 2; cancel_waits = 1;
  assert(netdev_ifup(&device.netdev) == -ECANCELED);
  assert(created == 2 && atomic_load(&alive[2]) && !device.netdev.d_flags);
  cancel_waits = 1;
  assert(netdev_ifup(&device.netdev) == -ECANCELED && created == 2);
  assert(netdev_ifup(&device.netdev) == 0 && created == 5);
  assert(netdev_ifdown(&device.netdev) == 0); cleanup();

  setup(NETDEV_RX_THREAD); real_worker = early_worker = true;
  assert(netdev_ifup(&device.netdev) == 0 && work_done == 1);
  assert(netdev_ifdown(&device.netdev) == 0);
  assert(pthread_join(worker, NULL) == 0); cleanup();
}
static void start_poll(void)
{
  pthread_mutex_lock(&control); launch = true;
  pthread_cond_broadcast(&changed); pthread_mutex_unlock(&control);
  nxsem_post(&current->thread[0].sem);
  pthread_mutex_lock(&control);
  while (!waiting_dev) pthread_cond_wait(&changed, &control);
  pthread_mutex_unlock(&control);
}
static void *unregister_thread(void *unused)
{
  (void)unused; assert(net_lock() == 0); assert(net_lock() == 0);
  assert(netdev_lower_unregister(&device) == 0);
  assert(g_netlock.count == 2); net_unlock(); net_unlock(); return NULL;
}
static void contended_case(void)
{
  setup(NETDEV_RX_THREAD); real_worker = true; assert(netdev_ifup(&device.netdev) == 0);
  assert(net_lock() == 0); assert(net_lock() == 0);
  netdev_lock(&device.netdev); netdev_lock(&device.netdev); start_poll();
  admission_probe = true;
  assert(netdev_ifdown(&device.netdev) == 0);
  assert(g_netlock.count == 2 && device.netdev.d_lock.count == 2 && work_done == 1);
  assert(pthread_join(worker, NULL) == 0);
  netdev_unlock(&device.netdev); netdev_unlock(&device.netdev); net_unlock(); net_unlock(); cleanup();

  setup(NETDEV_RX_THREAD); real_worker = true; assert(netdev_ifup(&device.netdev) == 0);
  netdev_lock(&device.netdev); start_poll();
  pthread_t removing; assert(pthread_create(&removing, NULL, unregister_thread, NULL) == 0);
  pthread_mutex_lock(&control);
  while (!waiting_exit) pthread_cond_wait(&changed, &control);
  pthread_mutex_unlock(&control); netdev_unlock(&device.netdev);
  assert(pthread_join(removing, NULL) == 0); assert(pthread_join(worker, NULL) == 0);
  assert(freed == 1 && unregistered == 1 && work_done == 1);
  assert(pthread_mutex_destroy(&g_netlock.mutex.lock) == 0);
}
int main(int argc, char **argv)
{
  assert(argc == 2);
  if (!strcmp(argv[1], "cancel")) cancel_case();
  else if (!strcmp(argv[1], "startup")) startup_case();
  else if (!strcmp(argv[1], "contended")) contended_case();
  else abort();
  return 0;
}
