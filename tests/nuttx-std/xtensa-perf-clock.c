/* Clock-frequency contract for the actual shared Xtensa perf conversion. */
#include <assert.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>

#define NSEC_PER_SEC 1000000000L
static unsigned long g_cpu_freq = ULONG_MAX;

/* FUNCTIONS */

int main(void)
{
  const unsigned long frequencies[] = { 80000000, 160000000, 240000000 };
  struct timespec result;
  for (unsigned int i = 0; i < sizeof(frequencies) / sizeof(frequencies[0]); i++)
    {
      up_perf_init((void *)(uintptr_t)frequencies[i]);
      assert(up_perf_getfreq() == frequencies[i]);
      up_perf_convert(frequencies[i], &result);
      assert(result.tv_sec == 1 && result.tv_nsec == 0);
      up_perf_convert(frequencies[i] + frequencies[i] / 2, &result);
      assert(result.tv_sec == 1 && result.tv_nsec == 500000000);
    }

  puts("Xtensa performance clock: configured-frequency conversion passes");
  return 0;
}
