/* NuttX qualification of local timer and remote scheduler interrupts. */
#include <errno.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <unistd.h>
#include <nuttx/arch.h>
#include <nuttx/clock.h>
#include <nuttx/sched.h>
#include <nuttx/semaphore.h>
#include <nuttx/wdog.h>

#if !defined(CONFIG_SMP) || CONFIG_SMP_NCPUS != 2
#  error This witness needs exactly two NuttX CPUs
#endif

struct witness
{
  sem_t ready;
  sem_t wake;
  struct wdog_s watchdog;
  atomic_uint progress;
  atomic_bool finished;
  atomic_bool preempted;
  unsigned int irq_progress;
  int irq_cpu;
  int high_cpu;
  int low_cpu;
  int timer_status;
  int wake_status;
  int cpu;
  bool timer;
  bool in_irq;
};

static int interrupt_wake(void *arg)
{
  struct witness *test = arg;

  test->irq_cpu = up_cpu_index();
  test->in_irq = up_interrupt_context();
  test->irq_progress = atomic_load(&test->progress);
  return nxsem_post(&test->wake);
}

static void timer_wake(wdparm_t arg)
{
  interrupt_wake((void *)arg);
}

static void *high_task(void *arg)
{
  struct witness *test = arg;

  sem_post(&test->ready);
  test->wake_status = sem_wait(&test->wake);
  test->high_cpu = up_cpu_index();
  atomic_store(&test->preempted, !atomic_load(&test->finished) &&
              atomic_load(&test->progress) > 0);
  return NULL;
}

static void *busy_task(void *arg)
{
  struct witness *test = arg;

  test->low_cpu = up_cpu_index();
  if (test->timer)
    {
      test->timer_status = wd_start(&test->watchdog, MSEC2TICK(25),
                                    timer_wake, (wdparm_t)test);
    }

  /* This loop makes no calls, yields or clock queries. A high-priority task
   * on this CPU has to interrupt it before it reaches the limit.
   */
  for (unsigned int i = 1; i <= 10000000; i++)
    {
      atomic_store(&test->progress, i);
      if (atomic_load(&test->preempted))
        {
          break;
        }
    }

  atomic_store(&test->finished, true);
  return NULL;
}

static int spawn(pthread_t *thread, int cpu, int priority,
                 void *(*entry)(void *), void *arg)
{
  cpu_set_t mask;
  pthread_attr_t attr;
  struct sched_param schedule = { .sched_priority = priority };
  int ret = pthread_attr_init(&attr);

  if (ret != 0)
    {
      return ret;
    }

  CPU_ZERO(&mask);
  CPU_SET(cpu, &mask);
  ret = pthread_attr_setaffinity_np(&attr, sizeof(mask), &mask);
  if (ret == 0)
    {
      ret = pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    }

  if (ret == 0)
    {
      ret = pthread_attr_setschedpolicy(&attr, SCHED_FIFO);
    }

  if (ret == 0)
    {
      ret = pthread_attr_setschedparam(&attr, &schedule);
    }

  if (ret == 0)
    {
      ret = pthread_create(thread, &attr, entry, arg);
    }

  pthread_attr_destroy(&attr);
  return ret;
}

static bool run_case(bool timer)
{
  struct witness test = { .timer = timer, .cpu = timer ? 0 : 1 };
  pthread_t high;
  pthread_t low;
  int send = 0;
  int ret;

  if (sem_init(&test.ready, 0, 0) < 0 || sem_init(&test.wake, 0, 0) < 0)
    {
      return false;
    }

  ret = spawn(&high, test.cpu, 120, high_task, &test);
  if (ret != 0)
    {
      printf("PREEMPT_SETUP high=%d\n", ret);
      return false;
    }

  if (sem_wait(&test.ready) < 0)
    {
      send = -errno;
      goto retire_high;
    }

  ret = spawn(&low, test.cpu, 80, busy_task, &test);
  if (ret != 0)
    {
      send = -ret;
      goto retire_high;
    }

  if (!timer)
    {
      for (unsigned int tick = 0; tick < 100 &&
           atomic_load(&test.progress) == 0; tick++)
        {
          usleep(1000);
        }

      send = nxsched_smp_call_single(test.cpu, interrupt_wake, &test);
    }

  pthread_join(low, NULL);
  if (timer)
    {
      wd_cancel(&test.watchdog);
    }

retire_high:
  /* If the interrupt never ran, let the waiter exit with finished already
   * set. The task-context fallback cannot satisfy the IRQ and overlap checks.
   */
  atomic_store(&test.finished, true);
  sem_post(&test.wake);
  pthread_join(high, NULL);
  sem_destroy(&test.wake);
  sem_destroy(&test.ready);

  bool pass = send == 0 && test.timer_status == 0 && test.wake_status == 0 &&
              test.in_irq && test.irq_progress > 0 &&
              test.irq_cpu == test.cpu && test.high_cpu == test.cpu &&
              test.low_cpu == test.cpu && atomic_load(&test.preempted);

  printf("PREEMPT_%s %s cpu=%d irq_cpu=%d progress=%u irq=%d status=%d\n",
         timer ? "TIMER" : "IPI", pass ? "PASS" : "FAIL", test.cpu,
         test.irq_cpu, test.irq_progress, test.in_irq, send);
  return pass;
}

static void *run_witness(void *arg)
{
  bool *passed = arg;

  if (up_cpu_index() != 0)
    {
      printf("PREEMPT_SETUP unexpected coordinator cpu=%d\n", up_cpu_index());
      return NULL;
    }

  bool timer = run_case(true);
  bool ipi = run_case(false);
  *passed = timer && ipi;
  return NULL;
}

int nsh_main(int argc, char **argv);

int preemption_main(int argc, char **argv)
{
  bool passed = false;
  pthread_t coordinator;
  int ret = spawn(&coordinator, 0, 100, run_witness, &passed);

  if (ret != 0)
    {
      printf("PREEMPT_SETUP coordinator=%d\n", ret);
      return 1;
    }

  pthread_join(coordinator, NULL);
  printf("PREEMPT_SMP %s\n", passed ? "PASS" : "FAIL");
  return nsh_main(argc, argv);
}
