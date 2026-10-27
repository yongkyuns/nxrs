/* Fixture-only target-header observations. Rust owns budgets and sampling. */
#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/clock.h>
#include <nuttx/sched.h>
#include <malloc.h>
#include <stdint.h>
#include <time.h>

#if !defined(CONFIG_STACK_COLORATION) || !defined(CONFIG_ARCH_PERF_EVENTS) || \
    !defined(CONFIG_PERF_OVERFLOW_CORRECTION) || \
    !defined(CONFIG_SCHED_CRITMONITOR) || \
    CONFIG_SCHED_CRITMONITOR_MAXTIME_THREAD < 0
#  error "TLS resource witness requires native stack coloring and thread accounting"
#endif

/* Called synchronously by the current thread while its own TCB is alive.
 * These are native-header scalar translations, never a C resource controller.
 * CPU time contains completed scheduler slices; ISR attribution follows NuttX.
 */
int nxrs_tls_test_resources(uint64_t values[10])
{
  FAR struct tcb_s *tcb = nxsched_self();
  struct mallinfo memory = mallinfo();
  struct timespec cpu;
  int result = clock_gettime(CLOCK_THREAD_CPUTIME_ID, &cpu);
  if (result != 0)
    {
      return result;
    }

  values[0] = up_check_tcbstack(tcb, tcb->adj_stack_size);
  values[1] = tcb->adj_stack_size;
  values[2] = memory.arena;
  values[3] = memory.uordblks;
  values[4] = memory.fordblks;
  values[5] = memory.usmblks;
  values[6] = memory.mxordblk;
  values[7] = (uint64_t)cpu.tv_sec * 1000000000 + cpu.tv_nsec;
  values[8] = up_perf_getfreq();
  values[9] = (uint64_t)CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ * 1000000;
  return 0;
}
