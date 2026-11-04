/* Mechanical NuttX note/TCB/IRQ translation, private to the P4 witness. */
#include <nuttx/config.h>
#include <nuttx/irq.h>
#include <nuttx/sched.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include "arch/xtensa/src/chip/esp32s3_spiram.h"
#include "arch/xtensa/src/chip/hardware/esp32s3_soc.h"

#if !defined(CONFIG_ARCH_CHIP_ESP32S3) || !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_SMP)
# error "P4 native bridge requires qualified single-core flat ESP32-S3"
#endif
#if defined(CONFIG_SCHED_INSTRUMENTATION_CSECTION) || defined(CONFIG_SCHED_INSTRUMENTATION_FILTER)
# error "P4 refuses recursive critical-section notes or unconfigured note filters"
#endif

/* Real SDK callbacks enter Rust's permanently allocated bounded recorder.
 * The recorder allocates/prints/waits nowhere in this callback path. */
typedef void (*trace_callback_t)(uint32_t kind, int tid, uint32_t detail);
static trace_callback_t g_callback;
extern bool __real_nxsched_add_readytorun(struct tcb_s *tcb);

static void trace_note(uint32_t kind, int tid, uint32_t detail)
{
  if (g_callback != NULL) g_callback(kind, tid, detail);
}

bool __wrap_nxsched_add_readytorun(struct tcb_s *tcb)
{
  bool preempt = __real_nxsched_add_readytorun(tcb);
  unsigned state = tcb->task_state == TSTATE_TASK_READYTORUN ? 1 :
                   tcb->task_state == TSTATE_TASK_RUNNING ? 2 :
                   tcb->task_state == TSTATE_TASK_PENDING ? 3 : 0;
  trace_note(1, tcb->pid, state);
  return preempt;
}

uintptr_t trace_irq_save(void) { return (uintptr_t)enter_critical_section(); }
void trace_irq_restore(uintptr_t flags) { leave_critical_section((irqstate_t)flags); }

int trace_stack(int tid, uintptr_t *base, uintptr_t *bytes)
{
  irqstate_t flags = enter_critical_section();
  struct tcb_s *tcb = nxsched_get_tcb(tid);
  if (tcb != NULL)
    {
      *base = (uintptr_t)tcb->stack_base_ptr;
      *bytes = tcb->adj_stack_size;
    }
  leave_critical_section(flags);
  return tcb == NULL ? ESRCH : 0;
}

unsigned trace_memory_region(uintptr_t base, uintptr_t bytes)
{
  if (bytes == 0 || base > UINTPTR_MAX - bytes) return 0;
  if (base >= SOC_DRAM_LOW && base + bytes <= SOC_DRAM_HIGH) return 1;
#ifdef CONFIG_ESP32S3_SPIRAM
  if (base >= esp_spiram_allocable_vaddr_start() &&
      base + bytes <= esp_spiram_allocable_vaddr_end()) return 2;
#endif
  return 0;
}

#ifdef CONFIG_SCHED_INSTRUMENTATION_SWITCH
# include <nuttx/note/note_driver.h>
# if !defined(CONFIG_DRIVERS_NOTE) || !defined(CONFIG_SCHED_INSTRUMENTATION_IRQHANDLER)
#  error "P4 trace image must supply switch and IRQ callbacks together"
# endif
static void trace_suspend(struct note_driver_s *driver, struct tcb_s *tcb)
{
  (void)driver;
  trace_note(2, tcb->pid, tcb->task_state == TSTATE_WAIT_SEM ? 1 : 0);
}
static void trace_resume(struct note_driver_s *driver, struct tcb_s *tcb)
{
  (void)driver;
  trace_note(3, tcb->pid, 0);
}
static void trace_irq(struct note_driver_s *driver, int irq, void *handler, bool enter)
{
  (void)driver;
  (void)handler;
  trace_note(enter ? 4 : 5, this_task()->pid, (uint32_t)irq);
}
static const struct note_driver_ops_s trace_ops =
{
  .suspend = trace_suspend, .resume = trace_resume, .irqhandler = trace_irq
};
/* Registration has no removal API. Both this native table and Rust callback
 * storage live for the entire boot; no borrowed context can be freed. */
static struct note_driver_s trace_driver = { .ops = &trace_ops };
int trace_register(trace_callback_t callback)
{
  irqstate_t flags = enter_critical_section();
  g_callback = callback;
  leave_critical_section(flags);
  return note_driver_register(&trace_driver);
}
unsigned trace_instrumented(void) { return 1; }
#else
int trace_register(trace_callback_t callback)
{
  irqstate_t flags = enter_critical_section();
  g_callback = callback;
  leave_critical_section(flags);
  return 0;
}
unsigned trace_instrumented(void) { return 0; }
#endif
