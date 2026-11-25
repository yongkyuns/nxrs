/* Test-only NuttX scheduling/wait/watchdog observations. Rust owns every
 * experiment, endpoint, thread, cancellation and join. */
#include <nuttx/config.h>
#include <nuttx/irq.h>
#include <nuttx/sched.h>
#include <nuttx/wdog.h>
#include <nuttx/clock.h>
#include <sched.h>
#include <stdint.h>
#include <errno.h>
#include <assert.h>
#include <unistd.h>

#if !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_SMP)
#  error "owner qualification requires flat single-core NuttX"
#endif
#ifdef CONFIG_PRIORITY_INHERITANCE
#  error "the selected owner witness requires unchanged effective FIFO priorities"
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
