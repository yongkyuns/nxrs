/* Target-header compute context access. Rust owns the scoped lease. */
#include <nuttx/config.h>
#include <stdint.h>
#include <sched.h>
#ifdef CONFIG_ARCH_CHIP_ESP32S3
#include "arch/xtensa/src/common/xtensa.h"
#include <arch/xtensa/xtensa_coproc.h>
#include <arch/chip/tie.h>
_Static_assert((XCHAL_CP_MASK & (1 << 3)) != 0 && XCHAL_CP3_SA_SIZE > 0,
               "ESP32-S3 numerical SDK needs saved cop_ai context");
#endif
int detector_compute_cpus(void)
{
#ifdef CONFIG_SMP
  return CONFIG_SMP_NCPUS;
#else
  return 1;
#endif
}
int detector_affinity_get(uint32_t *result)
{
  cpu_set_t mask = 0;
  _Static_assert(sizeof(cpu_set_t) <= sizeof(uint32_t), "affinity mask capacity");
  int status = sched_getaffinity(0, sizeof(mask), &mask);
  if (status == 0) *result = (uint32_t)mask;
  return status;
}
int detector_affinity_set(uint32_t value)
{
  cpu_set_t mask = (cpu_set_t)value;
  return sched_setaffinity(0, sizeof(mask), &mask);
}
int detector_current_cpu(void) { return sched_getcpu(); }
int detector_compute_enabled(void)
{
#ifdef CONFIG_ARCH_CHIP_ESP32S3
  return (xtensa_get_cpenable() & (1 << 3)) != 0;
#else
  return 0;
#endif
}
void detector_compute_set(int enabled)
{
#ifdef CONFIG_ARCH_CHIP_ESP32S3
  if (enabled)
    {
      xtensa_coproc_enable(1 << 3);
    }
  else
    {
      xtensa_coproc_disable(1 << 3);
    }
#endif
}
