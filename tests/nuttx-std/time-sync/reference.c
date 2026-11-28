/* Qualification-only observation of the independent emulated APB counter.
 * No clock mutation is implemented here: the fixture calls hal/time in Rust.
 * Compile against the configured target headers to translate clock_t exactly.
 */
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <stdint.h>

extern clock_t __wrap_up_perf_gettime(void);
extern unsigned long __wrap_up_perf_getfreq(void);

uint64_t time_reference_ns(void)
{
  return (uint64_t)(uint32_t)__wrap_up_perf_gettime() *
         (1000000000ul / __wrap_up_perf_getfreq());
}
