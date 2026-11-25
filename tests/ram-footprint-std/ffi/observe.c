/* Measurement-only target-header glue. No allocation, queues, lifecycle,
 * waiting, thread creation, or workload behavior belongs in this file. */
#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/sched.h>
#include <malloc.h>
#include <stddef.h>

#ifndef CONFIG_BUILD_FLAT
#  error "This internal-TCB diagnostic is qualified for a flat NuttX build only"
#endif
#ifndef CONFIG_STACK_COLORATION
#  error "RAM footprint qualification requires CONFIG_STACK_COLORATION=y"
#endif

struct ram_probe_heap_sample
{
  size_t used;
  size_t free;
  size_t arena;
};

struct ram_probe_stack_sample
{
  size_t usable;
  size_t observed;
};

void ram_probe_heap(struct ram_probe_heap_sample *out)
{
  struct mallinfo sample = mallinfo();
  out->used = sample.uordblks;
  out->free = sample.fordblks;
  out->arena = sample.arena;
}

void ram_probe_stack(struct ram_probe_stack_sample *out)
{
  struct tcb_s *self = nxsched_self();
  out->usable = self->adj_stack_size;
  out->observed = up_check_tcbstack(self, self->adj_stack_size);
}
