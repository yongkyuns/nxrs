/* Host-only test clock for the common gate; never linked in device builds. */
#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#include <time.h>
uint32_t nxrs_cq_heap_used(void) { return 0; }
uint32_t nxrs_cq_cycles(void)
{
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  uint64_t ns = (uint64_t)now.tv_sec * 1000000000u + now.tv_nsec;
  return (uint32_t)(ns * 240u / 1000u);
}
