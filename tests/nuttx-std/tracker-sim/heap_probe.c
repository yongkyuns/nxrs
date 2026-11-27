/* Link-only guard for the flat NuttX shared heap. Dedicated heaps and frees
 * remain available. No instrumentation is used in firmware product builds.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <nuttx/mm/mm.h>

volatile uint32_t tracker_heap_deny;
volatile uint32_t tracker_heap_rejected;
volatile uint32_t tracker_heap_last_bytes;
volatile uint32_t tracker_heap_last_caller;
volatile uint32_t tracker_heap_verified;

void *__real_mm_malloc(struct mm_heap_s *heap, size_t size);
void *__real_mm_memalign(struct mm_heap_s *heap, size_t alignment, size_t size);
void *__real_mm_realloc(struct mm_heap_s *heap, void *memory, size_t size);

static bool admit(struct mm_heap_s *heap, size_t size, void *caller)
{
  if (heap != g_mmheap || !tracker_heap_deny || size == 0)
    {
      return true;
    }

  tracker_heap_last_bytes = size;
  tracker_heap_last_caller = (uintptr_t)caller;
  __atomic_fetch_add(&tracker_heap_rejected, 1, __ATOMIC_RELAXED);
  return false;
}

void *__wrap_mm_malloc(struct mm_heap_s *heap, size_t size)
{
  return admit(heap, size, __builtin_return_address(0)) ?
         __real_mm_malloc(heap, size) : NULL;
}

void *__wrap_mm_memalign(struct mm_heap_s *heap, size_t alignment, size_t size)
{
  return admit(heap, size, __builtin_return_address(0)) ?
         __real_mm_memalign(heap, alignment, size) : NULL;
}

void *__wrap_mm_realloc(struct mm_heap_s *heap, void *memory, size_t size)
{
  return admit(heap, size, __builtin_return_address(0)) ?
         __real_mm_realloc(heap, memory, size) : NULL;
}

int tracker_heap_selftest(void)
{
  void *(*volatile allocate)(size_t) = malloc;
  void *memory = allocate(64);
  if (!memory)
    {
      return -1;
    }

  tracker_heap_deny = 1;
  void *allocated = allocate(32);
  void *grown = mm_realloc(g_mmheap, memory, 128);
  void *aligned = mm_memalign(g_mmheap, 32, 128);
  tracker_heap_deny = 0;

  tracker_heap_verified = !allocated && !grown && !aligned &&
                          tracker_heap_rejected == 3;
  free(allocated);
  free(grown ? grown : memory);
  mm_free(g_mmheap, aligned);
  tracker_heap_rejected = 0;
  tracker_heap_last_bytes = 0;
  tracker_heap_last_caller = 0;
  return tracker_heap_verified ? 0 : -1;
}
