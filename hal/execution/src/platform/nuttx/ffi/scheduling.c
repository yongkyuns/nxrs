/* Mechanical calling-thread scheduler ABI; no thread ownership/policy. */
#include <nuttx/config.h>
#include <sched.h>
int execution_current_priority(int priority)
{
  struct sched_param parameter = { .sched_priority = priority };
  return sched_setparam(0, &parameter);
}
