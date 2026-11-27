/* Host ABI stubs around the actual patched NuttX allocator sources. */
#include <assert.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <nuttx/debug.h>
#include "mm_heap/mm.h"
#include "outcome_notes.h"

#define ARENA_BYTES 16384
#define MAX_NOTES 4096

static pthread_mutex_t critical_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t note_mutex = PTHREAD_MUTEX_INITIALIZER;
static _Thread_local unsigned int critical_depth;
static _Thread_local unsigned int local_irq_depth;
static atomic_bool capture_enabled = true;
static bool disable_at_lock;
static bool enable_at_unlock;
static int test_panics;
static unsigned int note_count;
static atomic_uint_fast64_t clock_ticks;

struct tcb_s { int pid; };
static _Thread_local struct tcb_s current_task = { .pid = 1 };
struct note_driver_s;
struct note_driver_ops_s
{
  void (*add)(struct note_driver_s *, const void *, size_t);
};
struct note_driver_s { const struct note_driver_ops_s *ops; };

union recorded_note
{
  struct note_common_s common;
#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE
  struct note_heap_request_s request;
  struct note_heap_result_s result;
#endif
};

static union recorded_note notes[MAX_NOTES];

irqstate_t enter_critical_section_notrace(void)
{
  unsigned int previous = critical_depth;
  if (critical_depth++ == 0)
    {
      assert(pthread_mutex_lock(&critical_mutex) == 0);
    }
  return previous;
}

void leave_critical_section_notrace(irqstate_t state)
{
  assert(critical_depth == state + 1);
  critical_depth = state;
  if (critical_depth == 0)
    {
      assert(pthread_mutex_unlock(&critical_mutex) == 0);
    }
}

irqstate_t up_irq_save(void)
{
  return local_irq_depth++;
}

void up_irq_restore(irqstate_t state)
{
  assert(local_irq_depth == state + 1);
  local_irq_depth = state;
}

static void append_note(struct note_driver_s *driver, const void *record,
                        size_t size)
{
  (void)driver;
  assert(critical_depth == 0 && local_irq_depth > 0);
  assert(pthread_mutex_lock(&note_mutex) == 0);
  assert(size <= sizeof(notes[0]));
  assert(note_count < MAX_NOTES);
  memcpy(&notes[note_count++], record, size);
  assert(pthread_mutex_unlock(&note_mutex) == 0);
}

static const struct note_driver_ops_s note_ops = { .add = append_note };
static const struct note_driver_ops_s no_raw_ops = { .add = NULL };
static struct note_driver_s raw_driver = { .ops = &note_ops };
static struct note_driver_s no_raw_driver = { .ops = &no_raw_ops };
static struct note_driver_s *g_note_drivers[] =
  { &no_raw_driver, &raw_driver, NULL };

static bool note_isenabled(struct note_driver_s *driver)
{
  (void)driver;
  return atomic_load_explicit(&capture_enabled, memory_order_relaxed);
}

static struct tcb_s *this_task(void) { return &current_task; }

static void note_common(struct tcb_s *tcb, struct note_common_s *note,
                         size_t size, uint8_t type)
{
  assert(critical_depth == 0 && local_irq_depth > 0);
  assert(size <= UINT8_MAX);
  note->nc_type = type;
  note->nc_length = size;
  note->nc_pid = tcb->pid;
  note->nc_time = atomic_fetch_add(&clock_ticks, 1) + 1;
}

#define note_add(driver, record, size) ((driver)->ops->add(driver, record, size))
#include "outcome_driver.c"

int mm_lock(struct mm_heap_s *heap)
{
  assert(critical_depth == 0);
  assert(heap->mm_lock == 0);
  heap->mm_lock = 1;
  if (disable_at_lock)
    {
      atomic_store(&capture_enabled, false);
    }
  return 0;
}

void mm_unlock(struct mm_heap_s *heap)
{
  assert(critical_depth == 0 && heap->mm_lock == 1);
  heap->mm_lock = 0;
  if (enable_at_unlock)
    {
      atomic_store(&capture_enabled, true);
    }
}

irqstate_t mm_lock_irq(struct mm_heap_s *heap)
{
  (void)heap;
  return enter_critical_section_notrace();
}

void mm_unlock_irq(struct mm_heap_s *heap, irqstate_t state)
{
  (void)heap;
  leave_critical_section_notrace(state);
}

bool mm_heapmember(struct mm_heap_s *heap, const void *memory)
{
#ifdef CONFIG_MM_HEAP_MEMPOOL
  if (heap->mm_mpool &&
      mempool_multiple_alloc_size(heap->mm_mpool, (void *)memory) >= 0)
    return true;
#endif
  return (uintptr_t)memory >= (uintptr_t)heap->mm_heapstart[0] &&
         (uintptr_t)memory < (uintptr_t)heap->mm_heapend[0];
}

size_t mm_malloc_size(struct mm_heap_s *heap, void *memory)
{
#ifdef CONFIG_MM_HEAP_MEMPOOL
  if (heap->mm_mpool)
    {
      int pool_size = mempool_multiple_alloc_size(heap->mm_mpool, memory);
      if (pool_size >= 0) return pool_size;
    }
#endif
  (void)heap;
  struct mm_allocnode_s *node =
    (struct mm_allocnode_s *)((char *)memory - MM_SIZEOF_ALLOCNODE);
  return MM_SIZEOF_NODE(node) - MM_ALLOCNODE_OVERHEAD;
}

void sched_note_heap(uint8_t event, void *heap, void *memory,
                     size_t size, size_t used)
{
  (void)event;
  (void)heap;
  (void)memory;
  (void)size;
  (void)used;
}

#ifdef CONFIG_MM_HEAP_MEMPOOL
static unsigned char pool_storage[128] __attribute__((aligned(128)));
static unsigned int pool_frees;
void *mempool_multiple_alloc(struct mempool_multiple_s *pool, size_t size)
{ (void)pool; return size <= 64 ? pool_storage : NULL; }
void *mempool_multiple_memalign(struct mempool_multiple_s *pool,
                               size_t alignment, size_t size)
{ (void)pool; return alignment <= 128 && size <= 64 ? pool_storage : NULL; }
void *mempool_multiple_realloc(struct mempool_multiple_s *pool,
                              void *memory, size_t size)
{ (void)pool; return memory == pool_storage && size <= 64 ? memory : NULL; }
int mempool_multiple_alloc_size(struct mempool_multiple_s *pool, void *memory)
{ (void)pool; return memory == pool_storage ? 64 : -1; }
int mempool_multiple_free(struct mempool_multiple_s *pool, void *memory)
{
  (void)pool;
  if (memory != pool_storage) return -1;
  pool_frees++;
  return 0;
}
void mempool_info(struct mempool_s *pool, struct mempoolinfo_s *info)
{ (void)pool; memset(info, 0, sizeof(*info)); }
#endif

#include "mm_heap/mm_malloc.c"
#include "mm_heap/mm_free.c"
#include "mm_heap/mm_realloc.c"
#include "mm_heap/mm_memalign.c"
#include "mm_heap/mm_shrinkchunk.c"

static void initialize_heap(struct mm_heap_s *heap, void *arena)
{
  memset(heap, 0, sizeof(*heap));
  memset(arena, 0, ARENA_BYTES);
  for (unsigned int i = 1; i < MM_NNODES; i++)
    {
      heap->mm_nodelist[i - 1].flink = &heap->mm_nodelist[i];
      heap->mm_nodelist[i].blink = &heap->mm_nodelist[i - 1];
    }
  struct mm_allocnode_s *head = arena;
  struct mm_freenode_s *free_node =
    (struct mm_freenode_s *)((char *)arena + MM_MIN_CHUNK);
  struct mm_allocnode_s *tail =
    (struct mm_allocnode_s *)((char *)arena + ARENA_BYTES - MM_MIN_CHUNK);
  head->size = MM_MIN_CHUNK | MM_ALLOC_BIT;
  free_node->size = ARENA_BYTES - 2 * MM_MIN_CHUNK;
  tail->preceding = free_node->size;
  tail->size = MM_MIN_CHUNK | MM_ALLOC_BIT | MM_PREVFREE_BIT;
  heap->mm_heapstart[0] = head;
  heap->mm_heapend[0] = tail;
  heap->mm_heapsize = ARENA_BYTES;
  heap->mm_curused = heap->mm_maxused = 2 * MM_MIN_CHUNK;
  mm_addfreechunk(heap, free_node);
}

#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE
static unsigned int entries(uint8_t operation)
{
  unsigned int count = 0;
  for (unsigned int i = 0; i < note_count; i++)
    if (notes[i].common.nc_type == NOTE_HEAP_REQUEST &&
        notes[i].request.operation == operation) count++;
  return count;
}

static void assert_complete_pairs(void)
{
  assert(note_count > 0 && note_count % 2 == 0);
  for (unsigned int i = 0; i < note_count; i++)
    {
      assert(notes[i].common.nc_time > 0);
      if (notes[i].common.nc_type != NOTE_HEAP_REQUEST) continue;
      struct note_heap_request_s *request = &notes[i].request;
      assert(request->request_id != 0 && request->caller != NULL);
      unsigned int matches = 0;
      for (unsigned int j = i + 1; j < note_count; j++)
        if (notes[j].common.nc_type == NOTE_HEAP_RESULT &&
            notes[j].result.request_id == request->request_id)
          {
            assert(notes[j].result.heap == request->heap);
            assert(notes[j].result.operation == request->operation);
            assert(notes[j].common.nc_pid == notes[i].common.nc_pid);
            assert(notes[j].common.nc_time > notes[i].common.nc_time);
            matches++;
          }
      assert(matches == 1);
      for (unsigned int j = i + 1; j < note_count; j++)
        if (notes[j].common.nc_type == NOTE_HEAP_REQUEST)
          assert(notes[j].request.request_id != request->request_id);
    }
}

static void verify_last_call(uint8_t operation, size_t requested, void *oldmem,
                              size_t alignment, void *result)
{
  struct note_heap_result_s *end = &notes[note_count - 1].result;
  assert(end->nhr_cmn.nc_type == NOTE_HEAP_RESULT && end->operation == operation);
  assert(end->result == result);
  unsigned int i = note_count - 1;
  while (i-- > 0)
    if (notes[i].common.nc_type == NOTE_HEAP_REQUEST &&
        notes[i].request.request_id == end->request_id)
      {
        assert(notes[i].request.size == requested);
        assert(notes[i].request.oldmem == oldmem);
        assert(notes[i].request.alignment == alignment);
        return;
      }
  assert(!"missing request for last result");
}
#define CHECK_CALL(op, size, old, align, result) \
  verify_last_call(op, size, old, align, result)
#else
#define CHECK_CALL(op, size, old, align, result) ((void)0)
#endif

static void ordinary_calls(void)
{
  struct mm_heap_s heap;
  unsigned char arena[ARENA_BYTES] __attribute__((aligned(64)));
  initialize_heap(&heap, arena);
  void *a = mm_malloc(&heap, 1024);
  assert(a != NULL);
  CHECK_CALL(MM_REQUEST_MALLOC, 1024, NULL, 0, a);
  memset(a, 0x5a, 128);
  void *small = mm_realloc(&heap, a, 128);
  assert(small == a && ((unsigned char *)a)[0] == 0x5a);
  CHECK_CALL(MM_REQUEST_REALLOC, 128, a, 0, small);
  void *grown = mm_realloc(&heap, a, 2048);
  assert(grown == a && ((unsigned char *)grown)[127] == 0x5a);
  CHECK_CALL(MM_REQUEST_REALLOC, 2048, a, 0, grown);
  void *blocker = mm_malloc(&heap, 1024);
  void *moved = mm_realloc(&heap, a, 4096);
  assert(moved != NULL && moved != a && ((unsigned char *)moved)[127] == 0x5a);
  CHECK_CALL(MM_REQUEST_REALLOC, 4096, a, 0, moved);
  void *failed = mm_realloc(&heap, moved, ARENA_BYTES * 2);
  assert(failed == NULL && ((unsigned char *)moved)[127] == 0x5a);
  CHECK_CALL(MM_REQUEST_REALLOC, ARENA_BYTES * 2, moved, 0, failed);
  void *zero = mm_realloc(&heap, moved, 0);
  assert(zero == moved && ((unsigned char *)zero)[0] == 0x5a);
  CHECK_CALL(MM_REQUEST_REALLOC, 0, moved, 0, zero);
  void *empty = mm_malloc(&heap, 0);
  assert(empty != NULL);
  CHECK_CALL(MM_REQUEST_MALLOC, 0, NULL, 0, empty);
  void *from_null = mm_realloc(&heap, NULL, 32);
  assert(from_null != NULL);
  CHECK_CALL(MM_REQUEST_REALLOC, 32, NULL, 0, from_null);
  void *aligned = mm_memalign(&heap, 128, 48);
  assert(aligned != NULL && ((uintptr_t)aligned % 128) == 0);
  CHECK_CALL(MM_REQUEST_MEMALIGN, 48, NULL, 128, aligned);
  void *natural = mm_memalign(&heap, MM_ALIGN, 24);
  assert(natural != NULL);
  CHECK_CALL(MM_REQUEST_MEMALIGN, 24, NULL, MM_ALIGN, natural);
  assert(mm_memalign(&heap, 3, 48) == NULL);
  CHECK_CALL(MM_REQUEST_MEMALIGN, 48, NULL, 3, NULL);
  assert(mm_memalign(&heap, SIZE_MAX, 48) == NULL);
  CHECK_CALL(MM_REQUEST_MEMALIGN, 48, NULL, SIZE_MAX, NULL);
  assert(mm_memalign(&heap, 128, SIZE_MAX) == NULL);
  CHECK_CALL(MM_REQUEST_MEMALIGN, SIZE_MAX, NULL, 128, NULL);
  assert(mm_memalign(&heap, 128, ARENA_BYTES * 2) == NULL);
  CHECK_CALL(MM_REQUEST_MEMALIGN, ARENA_BYTES * 2, NULL, 128, NULL);
  assert(mm_malloc(&heap, SIZE_MAX) == NULL);
  CHECK_CALL(MM_REQUEST_MALLOC, SIZE_MAX, NULL, 0, NULL);
  mm_free(&heap, zero); mm_free(&heap, blocker); mm_free(&heap, empty);
  mm_free(&heap, from_null); mm_free(&heap, aligned); mm_free(&heap, natural);
#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE
  assert_complete_pairs();
  assert(mm_alloc_requests(&heap) == note_count / 2);
  assert(entries(MM_REQUEST_MALLOC) > 7); /* Nested native backing calls. */
  assert(entries(MM_REQUEST_REALLOC) == 6);
  assert(entries(MM_REQUEST_MEMALIGN) == 6);
#else
  assert(note_count == 0);
#endif
  assert(heap.mm_curused == 2 * MM_MIN_CHUNK);
  assert(test_panics == 0);
}

#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE
static void filtered_calls(void)
{
  struct mm_heap_s heap;
  unsigned char arena[ARENA_BYTES] __attribute__((aligned(64)));
  initialize_heap(&heap, arena);
  disable_at_lock = true;
  void *first = mm_malloc(&heap, 64);
  assert(first && note_count == 1);
  assert(notes[0].common.nc_type == NOTE_HEAP_REQUEST);
  disable_at_lock = false;
  enable_at_unlock = true;
  void *second = mm_malloc(&heap, 64);
  assert(second && note_count == 2);
  assert(notes[1].common.nc_type == NOTE_HEAP_RESULT);
  assert(notes[0].request.request_id != notes[1].result.request_id);
  assert(mm_alloc_requests(&heap) == 2);
}

static void reset_heap_identity(void)
{
  struct mm_heap_s heap;
  unsigned char arena[ARENA_BYTES] __attribute__((aligned(64)));
  initialize_heap(&heap, arena);
  assert(mm_malloc(&heap, 64));
  uint64_t first = notes[0].request.request_id;
  initialize_heap(&heap, arena);
  assert(mm_malloc(&heap, 64));
  assert(notes[2].request.request_id > first);
  assert(mm_alloc_requests(&heap) == 1);
  assert_complete_pairs();
}

static void saturated_identity(void)
{
  struct mm_heap_s heap;
  unsigned char arena[ARENA_BYTES] __attribute__((aligned(64)));
  initialize_heap(&heap, arena);
  g_mm_request_id = UINT64_MAX - 1;
  assert(mm_malloc(&heap, 64));
  assert(notes[0].request.request_id == UINT64_MAX && note_count == 2);
  assert(mm_malloc(&heap, 64));
  assert(mm_malloc(&heap, 64));
  assert(g_mm_request_id == UINT64_MAX && note_count == 2);
  assert(mm_alloc_requests(&heap) == 3);
  assert_complete_pairs();
}

static void delayed_retry(void)
{
  struct mm_heap_s heap;
  unsigned char arena[ARENA_BYTES] __attribute__((aligned(64)));
  initialize_heap(&heap, arena);
  void *full = mm_malloc(&heap, ARENA_BYTES - 2 * MM_MIN_CHUNK -
                               MM_ALLOCNODE_OVERHEAD);
  assert(full);
  mm_free(&heap, full);
  assert(heap.mm_curused == ARENA_BYTES);
  void *retried = mm_malloc(&heap, 64);
  assert(retried);
  CHECK_CALL(MM_REQUEST_MALLOC, 64, NULL, 0, retried);
  assert(mm_alloc_requests(&heap) == 3);
  assert(entries(MM_REQUEST_MALLOC) == 3);
  assert_complete_pairs();
}

static void *concurrent_worker(void *arg)
{
  current_task.pid = (int)(uintptr_t)arg;
  struct mm_heap_s heap;
  unsigned char arena[ARENA_BYTES] __attribute__((aligned(64)));
  initialize_heap(&heap, arena);
  for (int i = 0; i < 100; i++)
    {
      void *memory = mm_malloc(&heap, 64);
      assert(memory);
      mm_free(&heap, memory);
    }
  assert(mm_alloc_requests(&heap) == 100);
  return NULL;
}

static void concurrent_calls(void)
{
  pthread_t threads[4];
  for (uintptr_t i = 0; i < 4; i++)
    assert(pthread_create(&threads[i], NULL, concurrent_worker, (void *)(i + 1)) == 0);
  for (unsigned int i = 0; i < 4; i++) assert(pthread_join(threads[i], NULL) == 0);
  assert(note_count == 800 && g_mm_request_id == 400);
  assert_complete_pairs();
}

#ifdef CONFIG_MM_HEAP_MEMPOOL
static void pool_calls(void)
{
  struct mm_heap_s heap;
  struct mempool_multiple_s pool;
  unsigned char arena[ARENA_BYTES] __attribute__((aligned(64)));
  initialize_heap(&heap, arena);
  heap.mm_mpool = &pool;
  heap.mm_threshold = 64;
  void *first = mm_malloc(&heap, 32);
  assert(first == pool_storage);
  CHECK_CALL(MM_REQUEST_MALLOC, 32, NULL, 0, first);
  void *resized = mm_realloc(&heap, first, 48);
  assert(resized == first);
  CHECK_CALL(MM_REQUEST_REALLOC, 48, first, 0, resized);
  void *aligned = mm_memalign(&heap, 128, 48);
  assert(aligned == pool_storage);
  CHECK_CALL(MM_REQUEST_MEMALIGN, 48, NULL, 128, aligned);
  memset(pool_storage, 0x3c, 64);
  void *moved = mm_realloc(&heap, first, 256);
  assert(moved && moved != first && ((unsigned char *)moved)[63] == 0x3c);
  assert(pool_frees == 1);
  CHECK_CALL(MM_REQUEST_REALLOC, 256, first, 0, moved);
  assert_complete_pairs();
}
#endif
#endif

int main(int argc, char **argv)
{
  assert(argc == 2);
  if (strcmp(argv[1], "ordinary") == 0) ordinary_calls();
#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE
  else if (strcmp(argv[1], "filtered") == 0) filtered_calls();
  else if (strcmp(argv[1], "heap-reuse") == 0) reset_heap_identity();
  else if (strcmp(argv[1], "saturation") == 0) saturated_identity();
  else if (strcmp(argv[1], "concurrent") == 0) concurrent_calls();
  else if (strcmp(argv[1], "delayed") == 0) delayed_retry();
#ifdef CONFIG_MM_HEAP_MEMPOOL
  else if (strcmp(argv[1], "pool") == 0) pool_calls();
#endif
#endif
  else assert(!"unknown test case");
  puts("native allocator outcome witness passed");
  return 0;
}
