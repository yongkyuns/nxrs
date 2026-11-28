/* Test-only NuttX scheduling/wait/watchdog observations. Rust owns every
 * experiment, endpoint, thread, cancellation and join. */
#include <nuttx/config.h>
#include <nuttx/irq.h>
#include <nuttx/sched.h>
#include <nuttx/wdog.h>
#include <nuttx/clock.h>
#include <sched.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdint.h>
#include <errno.h>
#include <assert.h>
#include <unistd.h>

#if !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_SMP)
#  error "owner qualification requires flat single-core NuttX"
#endif

static struct wdog_s qualification_watchdog;

int owner_qualification_schedule(int *values)
{
  struct sched_param parameter;
  int policy = sched_getscheduler(0);
  if (policy < 0 || sched_getparam(0, &parameter) < 0)
    return errno;
  values[0] = (int)gettid();
  values[1] = policy;
  values[2] = parameter.sched_priority;
  return 0;
}

int owner_qualification_set_schedule(int policy, int priority)
{
  struct sched_param parameter = { .sched_priority = priority };
  return sched_setscheduler(0, policy < 0 ? SCHED_FIFO : policy, &parameter) < 0
             ? errno : 0;
}

int owner_qualification_fifo(void) { return SCHED_FIFO; }

int owner_qualification_waiting(int tid, int priority)
{
  irqstate_t flags = enter_critical_section();
  struct tcb_s *tcb = nxsched_get_tcb((pid_t)tid);
  int waiting = tcb != NULL && tcb->task_state == TSTATE_WAIT_SEM &&
                tcb->sched_priority == priority &&
                (tcb->flags & TCB_FLAG_POLICY_MASK) == TCB_FLAG_SCHED_FIFO;
  leave_critical_section(flags);
  return waiting;
}

/* Target-header readback only. Rust selects and executes the experiment. The
 * actual std mutex stays opaque: these are freshly initialized default attrs,
 * corroborated by the source-bound std implementation and observed donation. */
int owner_qualification_mutex_configuration(int *values)
{
  pthread_mutexattr_t attributes;
  int protocol;
  int status = pthread_mutexattr_init(&attributes);
  if (status != 0)
    return status;
  status = pthread_mutexattr_getprotocol(&attributes, &protocol);
  int destroy_status = pthread_mutexattr_destroy(&attributes);
  if (status != 0 || destroy_status != 0)
    return status != 0 ? status : destroy_status;
#ifdef CONFIG_PRIORITY_INHERITANCE
  values[0] = 1;
#else
  values[0] = 0;
#endif
#ifdef CONFIG_PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT
  values[1] = 1;
#else
  values[1] = 0;
#endif
  values[2] = protocol;
  values[3] = PTHREAD_PRIO_NONE;
  values[4] = PTHREAD_PRIO_INHERIT;
#ifdef CONFIG_PRIORITY_INHERITANCE
  values[5] = CONFIG_SEM_PREALLOCHOLDERS;
#else
  values[5] = -1;
#endif
  return 0;
}

int owner_qualification_thread_state(int tid, int *values)
{
  int policy = sched_getscheduler((pid_t)tid);
  if (policy < 0)
    return errno;
  irqstate_t flags = enter_critical_section();
  struct tcb_s *tcb = nxsched_get_tcb((pid_t)tid);
  if (tcb == NULL)
    {
      leave_critical_section(flags);
      return ESRCH;
    }
  values[0] = policy;
  values[1] = tcb->sched_priority;
#ifdef CONFIG_PRIORITY_INHERITANCE
  values[2] = tcb->base_priority;
#else
  values[2] = tcb->sched_priority;
#endif
  values[3] = tcb->task_state;
  values[4] = tcb->task_state == TSTATE_WAIT_SEM;
  values[5] = tcb->task_state >= FIRST_READY_TO_RUN_STATE &&
              tcb->task_state <= LAST_READY_TO_RUN_STATE;
  leave_critical_section(flags);
  return 0;
}

/* Count header-owned tracking records for one task; never inspect an opaque
 * Rust mutex or infer its private address/layout. The Rust coordinator calls
 * this only while the observed task is parked in a stable fixture phase. */
int owner_qualification_pi_holder_count(int tid)
{
  irqstate_t flags = enter_critical_section();
  struct tcb_s *tcb = nxsched_get_tcb((pid_t)tid);
  int count = 0;
  if (tcb == NULL)
    {
      leave_critical_section(flags);
      return -ESRCH;
    }
#ifdef CONFIG_PRIORITY_INHERITANCE
  const struct semholder_s *holder;
  for (holder = tcb->holdsem; holder != NULL; holder = holder->tlink)
    count++;
#endif
  leave_critical_section(flags);
  return count;
}

static void qualification_expired(wdparm_t ignored)
{
  (void)ignored;
  PANIC();
}

int owner_qualification_watchdog_start(unsigned milliseconds)
{
  return wd_start(&qualification_watchdog, MSEC2TICK(milliseconds),
                  qualification_expired, 0);
}

int owner_qualification_watchdog_stop(void)
{
  return wd_cancel(&qualification_watchdog);
}
