/* Dedicated, normal priority-1 SYSTIMER target1 observer. The pinned single-core
 * images reserve target0 for OS ticks and target2 for esp_timer. Counter0 is
 * already running at 16 MHz: never reset it or alter its clock/stall policy.
 * Register/interrupt types below come from each image's real native SDK.
 */
#ifndef MPSC_IRQ_HOST_TEST
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <soc/systimer_reg.h>
#include <soc/interrupt_core0_reg.h>
#include <soc/interrupts.h>
#ifdef __NuttX__
#include <nuttx/config.h>
#include <nuttx/irq.h>
#include <arch/chip/core-isa.h>
#include "esp_irq.h"
#if defined(CONFIG_SMP) || !defined(CONFIG_ARCH_CHIP_ESP32S3)
#error "MPSC IRQ observer requires single-core ESP32-S3"
#endif
#define MP_IRQ_LOCK() up_irq_save()
#define MP_IRQ_UNLOCK(flags) up_irq_restore(flags)
#define MP_IRQ_OS_CLAIMED() (esp_get_cpuint(0, ESP32S3_IRQ_SYSTIMER_TARGET1) >= 0)
#else
#include <zephyr/kernel.h>
#include <zephyr/drivers/interrupt_controller/intc_esp32.h>
#include <xtensa/config/core-isa.h>
#if defined(CONFIG_SMP) || !defined(CONFIG_SOC_ESP32S3)
#error "MPSC IRQ observer requires single-core ESP32-S3"
#endif
#define MP_IRQ_LOCK() irq_lock()
#define MP_IRQ_UNLOCK(flags) irq_unlock(flags)
/* esp_intr_alloc refuses a claimed nonshared source, including one disabled
 * by its owner. The matrix-register check also catches external SDK mappings. */
#define MP_IRQ_OS_CLAIMED() false
#endif
#define MP_IRQ_READ(address) (*(volatile uint32_t *)(uintptr_t)(address))
#define MP_IRQ_WRITE(address, value) (*(volatile uint32_t *)(uintptr_t)(address) = (value))
#define MP_IRQ_IRAM __attribute__((section(".iram1.mpsc_irq_probe"), noinline))
#define MP_IRQ_INTERNAL(function) ((uintptr_t)(function) >= 0x40370000u && (uintptr_t)(function) < 0x403e0000u)
#endif

static intr_handle_t mp_irq_handle;
static void (*mp_irq_callback)(uint64_t);
static bool mp_irq_active;
static uint32_t mp_irq_saved_conf, mp_irq_saved_hi, mp_irq_saved_lo;

static inline __attribute__((always_inline)) void mp_irq_bits(uint32_t reg, uint32_t clear, uint32_t set)
{
  MP_IRQ_WRITE(reg, (MP_IRQ_READ(reg) & ~clear) | set);
}

MP_IRQ_IRAM uint64_t mp_irq_probe_now(void)
{
  /* Bounded hardware-latch reads only; no RTOS wait, allocation or logging.
   * hi/lo/hi tolerates a higher-priority reader refreshing the shared latch. */
  MP_IRQ_WRITE(SYSTIMER_UNIT0_OP_REG,
               MP_IRQ_READ(SYSTIMER_UNIT0_OP_REG) | SYSTIMER_TIMER_UNIT0_UPDATE);
  for (unsigned attempt = 0; attempt < 16; ++attempt)
    {
      if (MP_IRQ_READ(SYSTIMER_UNIT0_OP_REG) & SYSTIMER_TIMER_UNIT0_VALUE_VALID)
        {
          uint32_t hi = MP_IRQ_READ(SYSTIMER_UNIT0_VALUE_HI_REG) & SYSTIMER_TIMER_UNIT0_VALUE_HI;
          uint32_t lo = MP_IRQ_READ(SYSTIMER_UNIT0_VALUE_LO_REG);
          if (hi == (MP_IRQ_READ(SYSTIMER_UNIT0_VALUE_HI_REG) & SYSTIMER_TIMER_UNIT0_VALUE_HI))
            return ((uint64_t)hi << 32) | lo;
        }
    }
  return UINT64_MAX;
}

MP_IRQ_IRAM int mp_irq_probe_arm(uint64_t deadline)
{
  if (!mp_irq_active || deadline >> 52)
    return -1;
  unsigned flags = MP_IRQ_LOCK();
  mp_irq_bits(SYSTIMER_CONF_REG, SYSTIMER_TARGET1_WORK_EN, 0);
  MP_IRQ_WRITE(SYSTIMER_TARGET1_HI_REG, (uint32_t)(deadline >> 32));
  MP_IRQ_WRITE(SYSTIMER_TARGET1_LO_REG, (uint32_t)deadline);
  MP_IRQ_WRITE(SYSTIMER_COMP1_LOAD_REG, SYSTIMER_TIMER_COMP1_LOAD);
  mp_irq_bits(SYSTIMER_INT_ENA_REG, 0, SYSTIMER_TARGET1_INT_ENA);
  mp_irq_bits(SYSTIMER_CONF_REG, 0, SYSTIMER_TARGET1_WORK_EN);
  MP_IRQ_UNLOCK(flags);
  /* S3 compensates an already-passed alarm. A delayed rearm fires immediately;
   * the Rust protocol advances by missed periods instead of a catch-up loop. */
  return 0;
}

MP_IRQ_IRAM void mp_irq_probe_isr(void *unused)
{
  uint64_t entered = mp_irq_probe_now(); /* first probe operation after dispatch */
  (void)unused;
  unsigned flags = MP_IRQ_LOCK();
  mp_irq_bits(SYSTIMER_CONF_REG, SYSTIMER_TARGET1_WORK_EN, 0);
  MP_IRQ_WRITE(SYSTIMER_INT_CLR_REG, SYSTIMER_TARGET1_INT_CLR);
  MP_IRQ_UNLOCK(flags);
  if (mp_irq_active)
    mp_irq_callback(entered);
}

static bool mp_irq_target_busy(void)
{
  return (MP_IRQ_READ(SYSTIMER_CONF_REG) & SYSTIMER_TARGET1_WORK_EN) ||
         (MP_IRQ_READ(SYSTIMER_INT_ENA_REG) & SYSTIMER_TARGET1_INT_ENA) ||
         (MP_IRQ_READ(SYSTIMER_INT_RAW_REG) & SYSTIMER_TARGET1_INT_RAW);
}

int mp_irq_probe_open(void (*callback)(uint64_t), uint32_t *info)
{
  unsigned flags = MP_IRQ_LOCK();
  uint32_t map = MP_IRQ_READ(INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG);
  uint32_t clock = MP_IRQ_READ(SYSTIMER_CONF_REG);
  /* Reset routes to internal timer2 (16), disabled SDK routes to timer0 (6).
   * Any external CPU route or NuttX handle is evidence of another owner. */
  bool unavailable = mp_irq_handle || !callback || MP_IRQ_OS_CLAIMED() ||
      mp_irq_target_busy() || ((map & 31u) != 16u && (map & 31u) != 6u) ||
      (clock & (SYSTIMER_CLK_EN | SYSTIMER_TIMER_UNIT0_WORK_EN)) !=
      (SYSTIMER_CLK_EN | SYSTIMER_TIMER_UNIT0_WORK_EN);
  MP_IRQ_UNLOCK(flags);
  if (unavailable)
    return -1;
  if (!MP_IRQ_INTERNAL(callback) || !MP_IRQ_INTERNAL(mp_irq_probe_isr) ||
      !MP_IRQ_INTERNAL(mp_irq_probe_now) || !MP_IRQ_INTERNAL(mp_irq_probe_arm))
    return -2;
  /* The allocator independently refuses an already claimed nonshared source.
   * Allocation is outside the ISR and outside our local IRQ lock. */
  intr_handle_t handle = NULL;
  int status = esp_intr_alloc(ETS_SYSTIMER_TARGET1_INTR_SOURCE,
      ESP_INTR_FLAG_LEVEL1 | ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_INTRDISABLED,
      mp_irq_probe_isr, NULL, &handle);
  if (status || !handle)
    return status ? status : -3;
  int cpu = esp_intr_get_cpu(handle), cpuint = esp_intr_get_intno(handle);
  flags = MP_IRQ_LOCK();
  unavailable = mp_irq_target_busy() || cpu != 0 || cpuint < 0 || cpuint >= 32 ||
      !(XCHAL_INTLEVEL1_MASK & (1u << cpuint));
  MP_IRQ_UNLOCK(flags);
  if (unavailable)
    {
      status = esp_intr_free(handle);
      if (status) { mp_irq_handle = handle; return -4; }
      return -5;
    }
  mp_irq_saved_conf = MP_IRQ_READ(SYSTIMER_TARGET1_CONF_REG);
  mp_irq_saved_hi = MP_IRQ_READ(SYSTIMER_TARGET1_HI_REG);
  mp_irq_saved_lo = MP_IRQ_READ(SYSTIMER_TARGET1_LO_REG);
  mp_irq_handle = handle;
  mp_irq_callback = callback;
  info[0] = (uint32_t)cpuint; info[1] = (uint32_t)cpu;
  info[2] = ETS_SYSTIMER_TARGET1_INTR_SOURCE;
  info[3] = (uint32_t)(uintptr_t)mp_irq_probe_isr;
  info[4] = (uint32_t)(uintptr_t)callback;
  info[5] = (uint32_t)(uintptr_t)mp_irq_probe_now;
  info[6] = (uint32_t)(uintptr_t)mp_irq_probe_arm;
  return 0;
}

int mp_irq_probe_start(uint64_t deadline)
{
  if (!mp_irq_handle || mp_irq_active || deadline >> 52 || mp_irq_target_busy())
    return -1;
  unsigned flags = MP_IRQ_LOCK();
  /* Preserve reserved fields; target1 is exclusively ours while acquired. */
  mp_irq_bits(SYSTIMER_TARGET1_CONF_REG,
              SYSTIMER_TARGET1_PERIOD_MODE | SYSTIMER_TARGET1_TIMER_UNIT_SEL, 0);
  mp_irq_active = true;
  int status = mp_irq_probe_arm(deadline);
  if (!status) status = esp_intr_enable(mp_irq_handle);
  if (status)
    {
      mp_irq_active = false;
      mp_irq_bits(SYSTIMER_CONF_REG, SYSTIMER_TARGET1_WORK_EN, 0);
      mp_irq_bits(SYSTIMER_INT_ENA_REG, SYSTIMER_TARGET1_INT_ENA, 0);
      MP_IRQ_WRITE(SYSTIMER_INT_CLR_REG, SYSTIMER_TARGET1_INT_CLR);
    }
  MP_IRQ_UNLOCK(flags);
  return status;
}

int mp_irq_probe_stop(uint64_t *end, uint32_t *pending)
{
  if (!mp_irq_handle || !mp_irq_active)
    return -1;
  unsigned flags = MP_IRQ_LOCK();
  /* A task cannot run concurrently with this ISR on the required single core.
   * Block delivery before touching Rust state; disable/ack only our source. */
  mp_irq_bits(SYSTIMER_CONF_REG, SYSTIMER_TARGET1_WORK_EN, 0);
  mp_irq_bits(SYSTIMER_INT_ENA_REG, SYSTIMER_TARGET1_INT_ENA, 0);
  *end = mp_irq_probe_now();
  *pending = !!(MP_IRQ_READ(SYSTIMER_INT_RAW_REG) & SYSTIMER_TARGET1_INT_RAW);
  mp_irq_active = false;
  int status = esp_intr_disable(mp_irq_handle);
  MP_IRQ_WRITE(SYSTIMER_INT_CLR_REG, SYSTIMER_TARGET1_INT_CLR);
  MP_IRQ_UNLOCK(flags);
  return status;
}

int mp_irq_probe_close(void)
{
  if (!mp_irq_handle || mp_irq_active || mp_irq_target_busy())
    return -1;
  unsigned flags = MP_IRQ_LOCK();
  MP_IRQ_WRITE(SYSTIMER_TARGET1_CONF_REG, mp_irq_saved_conf);
  MP_IRQ_WRITE(SYSTIMER_TARGET1_HI_REG, mp_irq_saved_hi);
  MP_IRQ_WRITE(SYSTIMER_TARGET1_LO_REG, mp_irq_saved_lo);
  MP_IRQ_WRITE(SYSTIMER_COMP1_LOAD_REG, SYSTIMER_TIMER_COMP1_LOAD);
  int status = mp_irq_target_busy() ? -2 : 0;
  MP_IRQ_UNLOCK(flags);
  if (status) return status;
  /* Restore while still owning the disabled source, then release it. Leave
   * the SDK's disabled matrix route: writing after free could overwrite a new
   * owner's mapping if a higher-priority task acquired the same source. */
  status = esp_intr_free(mp_irq_handle);
  if (status) return status; /* retain ownership on failed release */
  mp_irq_handle = NULL;
  mp_irq_callback = NULL;
  return status;
}
