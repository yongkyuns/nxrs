/* Target-header observations only. Rust owns the finite experiment and joins. */
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/sched.h>
#include <nuttx/wdog.h>
#include <assert.h>
#include <errno.h>
#include <sched.h>
#include <semaphore.h>
#include <unistd.h>

#if !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_SMP) || defined(CONFIG_PRIORITY_INHERITANCE)
#  error "std comparison requires flat single-core FIFO without priority inheritance"
#endif

static sem_t gates[2];
static struct wdog_s watchdog;

int std_compare_tid(void) { return gettid(); }
int std_compare_fifo(void) { return SCHED_FIFO; }
int std_compare_schedule(int tid, int *values)
{
  struct sched_param parameter;
  int policy = sched_getscheduler(tid);
  if (policy < 0 || sched_getparam(tid, &parameter) < 0)
    return errno;
  values[0] = policy;
  values[1] = parameter.sched_priority;
  return 0;
}

int std_compare_set_schedule(int tid, int policy, int priority)
{
  struct sched_param parameter = { .sched_priority = priority };
  return sched_setscheduler(tid, policy, &parameter) < 0 ? errno : 0;
}

int std_compare_state(int tid, int priority, int waiting)
{
  irqstate_t flags = enter_critical_section();
  struct tcb_s *tcb = nxsched_get_tcb(tid);
  int state = tcb != NULL && tcb->sched_priority == priority &&
              (tcb->flags & TCB_FLAG_POLICY_MASK) == TCB_FLAG_SCHED_FIFO &&
              tcb->task_state == (waiting ? TSTATE_WAIT_SEM : TSTATE_TASK_READYTORUN);
  leave_critical_section(flags);
  return state;
}

int std_compare_gate(int index, int operation)
{
  if (index < 0 || index >= 2)
    return EINVAL;
  int result;
  switch (operation) {
    case 0: result = sem_init(&gates[index], 0, 0); break;
    case 1: do { result = sem_wait(&gates[index]); } while (result < 0 && errno == EINTR); break;
    case 2: result = sem_post(&gates[index]); break;
    case 3: result = sem_destroy(&gates[index]); break;
    default: return EINVAL;
  }
  return result < 0 ? errno : 0;
}

static void expired(wdparm_t ignored) { (void)ignored; PANIC(); }
int std_compare_watchdog(int start)
{
  return start ? wd_start(&watchdog, MSEC2TICK(60000), expired, 0) : wd_cancel(&watchdog);
}
