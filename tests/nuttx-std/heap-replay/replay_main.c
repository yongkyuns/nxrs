/* Isolated default-allocator qualification; never linked into a product.
 * --wrap=mm_lock / --wrap=mm_unlock retain every admitted committed phase,
 * including no-op releases and memalign's temporary backing allocation.
 * Geometry is observed with GDB reads only. Attribution is deliberately absent:
 * NuttX may write PID/backtrace metadata after the allocator unlocks.
 */
#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/mm/mm.h>

#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#if defined(CONFIG_SMP) || !defined(CONFIG_MM_DEFAULT_MANAGER) || \
    !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_MM_HEAP_MEMPOOL)
#  error This witness admits one CPU and the flat default allocator without pools
#endif

#define ARENA_SIZE (64 * 1024)
#define MAX_TRANSACTIONS 256

/* Private allocator ABI is used only by this SDK witness. Its exact source
 * revision, signatures and final linked call sites are independently admitted.
 */
int __real_mm_lock(struct mm_heap_s *heap);
void __real_mm_unlock(struct mm_heap_s *heap);
void mm_delayfree(struct mm_heap_s *heap, void *mem, bool delay);
int timing_clock_initialize(void);
void timing_clock_stop(void);

uint8_t g_replay_arena[ARENA_SIZE] __attribute__((aligned(64)));
struct mm_heap_s *g_replay_heap;
char g_replay_recording_id[33];
void *g_replay_blocks[4];
volatile uint32_t g_replay_kind;
volatile uint32_t g_replay_sequence;
volatile uint32_t g_replay_operation;
volatile uint32_t g_replay_ticks;
volatile uint32_t g_replay_fault;
volatile uint32_t g_replay_begin_count;
volatile uint32_t g_replay_commit_count;
volatile uint32_t g_replay_safe_count;
volatile uint32_t g_replay_probe_sequence;
volatile uint32_t g_replay_primask;
volatile int g_replay_owner;
static bool g_replay_active;
static bool g_replay_locked;

/* Kinds: 1 initial, 2 acquired/begin, 3 pre-release/commit, 4 independent
 * application safe point, 5 final. Keep one retained actual entry address.
 */
void __attribute__((noinline)) replay_probe_stop(void)
{
  __asm__ __volatile__("" ::: "memory");
}

static void probe(uint32_t kind)
{
  irqstate_t flags = enter_critical_section();
  uint8_t primask = getprimask();
  cpsid();
  g_replay_kind = kind;
  g_replay_primask = getprimask();
  g_replay_probe_sequence++;
  g_replay_ticks = (uint32_t)perf_gettime();
  replay_probe_stop();
  setprimask(primask);
  leave_critical_section(flags);
}

int __wrap_mm_lock(struct mm_heap_s *heap)
{
  int result = __real_mm_lock(heap);
  if (g_replay_active && heap == g_replay_heap)
    {
      if (result < 0 || up_interrupt_context() || gettid() != g_replay_owner ||
          g_replay_locked || g_replay_begin_count >= MAX_TRANSACTIONS)
        {
          g_replay_fault |= 1;
        }
      else
        {
          g_replay_locked = true;
          g_replay_begin_count++;
          probe(2);
        }
    }
  return result;
}

void __wrap_mm_unlock(struct mm_heap_s *heap)
{
  if (g_replay_active && heap == g_replay_heap)
    {
      if (!g_replay_locked || up_interrupt_context() ||
          gettid() != g_replay_owner)
        {
          g_replay_fault |= 2;
        }
      else
        {
          g_replay_sequence++;
          g_replay_commit_count++;
          probe(3);
          g_replay_locked = false;
        }
    }
  __real_mm_unlock(heap);
}

static void safe_point(uint32_t operation)
{
  if (g_replay_locked || g_replay_begin_count != g_replay_commit_count)
    {
      g_replay_fault |= 4;
    }
  g_replay_operation = operation;
  g_replay_safe_count++;
  probe(4);
}

static bool arguments(int argc, char *argv[])
{
  if (argc != 2 || strlen(argv[1]) != 32)
    {
      return false;
    }
  for (unsigned int i = 0; i < 32; i++)
    {
      if (!((argv[1][i] >= '0' && argv[1][i] <= '9') ||
            (argv[1][i] >= 'a' && argv[1][i] <= 'f')))
        {
          return false;
        }
    }
  memcpy(g_replay_recording_id, argv[1], 33);
  return true;
}

#define CHECK(expression) do { if (!(expression)) { \
  g_replay_fault |= 8; goto done; } } while (0)
#define STEP(id, expression) do { g_replay_operation = id; expression; \
  safe_point(id); CHECK(g_replay_fault == 0); } while (0)

int main(int argc, char *argv[])
{
  void *old;
  void *result;
  if (!arguments(argc, argv) || timing_clock_initialize() < 0)
    {
      printf("NXRS_REPLAY FAIL reason=arguments_or_clock\n");
      return 1;
    }
  g_replay_heap = mm_initialize("replay-isolated", g_replay_arena, ARENA_SIZE);
  if (g_replay_heap == NULL)
    {
      timing_clock_stop();
      printf("NXRS_REPLAY FAIL reason=heap_initialization\n");
      return 1;
    }
  g_replay_owner = gettid();
  g_replay_active = true;
  probe(1);

  STEP(1, g_replay_blocks[0] = mm_malloc(g_replay_heap, 1024));
  CHECK(g_replay_blocks[0]);
  STEP(2, g_replay_blocks[1] = mm_malloc(g_replay_heap, 2048));
  CHECK(g_replay_blocks[1]);
  STEP(3, g_replay_blocks[2] = mm_malloc(g_replay_heap, 1024));
  CHECK(g_replay_blocks[2]);
  STEP(4, mm_free(g_replay_heap, g_replay_blocks[1]); g_replay_blocks[1] = NULL);
  STEP(5, g_replay_blocks[1] = mm_malloc(g_replay_heap, 1024));
  CHECK(g_replay_blocks[1]);
  STEP(6, mm_free(g_replay_heap, g_replay_blocks[0]); g_replay_blocks[0] = NULL);
  STEP(7, mm_free(g_replay_heap, g_replay_blocks[1]); g_replay_blocks[1] = NULL);
  STEP(8, mm_free(g_replay_heap, g_replay_blocks[2]); g_replay_blocks[2] = NULL);

  STEP(9, g_replay_blocks[0] = mm_malloc(g_replay_heap, 4096));
  CHECK(g_replay_blocks[0]);
  memset(g_replay_blocks[0], 0x5a, 512);
  old = g_replay_blocks[0];
  STEP(10, g_replay_blocks[0] = mm_realloc(g_replay_heap, old, 1024));
  CHECK(g_replay_blocks[0] == old);
  STEP(11, g_replay_blocks[0] = mm_realloc(g_replay_heap, old, 3072));
  CHECK(g_replay_blocks[0] == old);
  STEP(12, g_replay_blocks[1] = mm_malloc(g_replay_heap, 4096));
  CHECK(g_replay_blocks[1]);
  STEP(13, g_replay_blocks[0] = mm_realloc(g_replay_heap, old, 8192));
  CHECK(g_replay_blocks[0] && g_replay_blocks[0] != old);
  CHECK(((uint8_t *)g_replay_blocks[0])[0] == 0x5a &&
        ((uint8_t *)g_replay_blocks[0])[511] == 0x5a);
  old = g_replay_blocks[0];
  STEP(14, result = mm_realloc(g_replay_heap, old, ARENA_SIZE * 2));
  CHECK(result == NULL && ((uint8_t *)old)[0] == 0x5a);
  STEP(15, mm_free(g_replay_heap, g_replay_blocks[1]); g_replay_blocks[1] = NULL);
  STEP(16, mm_free(g_replay_heap, g_replay_blocks[0]); g_replay_blocks[0] = NULL);

  STEP(17, g_replay_blocks[0] = mm_memalign(g_replay_heap, 256, 512));
  CHECK(g_replay_blocks[0] && ((uintptr_t)g_replay_blocks[0] & 255) == 0);
  STEP(18, mm_free(g_replay_heap, g_replay_blocks[0]); g_replay_blocks[0] = NULL);
  STEP(19, g_replay_blocks[0] = mm_malloc(g_replay_heap, 512));
  CHECK(g_replay_blocks[0]);
  old = g_replay_blocks[0];
  STEP(20, mm_free(g_replay_heap, old); g_replay_blocks[0] = NULL);
  STEP(21, g_replay_blocks[0] = mm_malloc(g_replay_heap, 512));
  CHECK(g_replay_blocks[0] == old);
  /* Exercise the exact delayed-free implementation without admitting IRQ
   * entry into the isolated heap. Geometry stays allocated at operation 22.
   */
  STEP(22, mm_delayfree(g_replay_heap, old, true); g_replay_blocks[0] = NULL);
  STEP(23, g_replay_blocks[0] = mm_malloc(g_replay_heap, 256));
  CHECK(g_replay_blocks[0]);
  STEP(24, mm_free(g_replay_heap, g_replay_blocks[0]); g_replay_blocks[0] = NULL);

done:
  probe(5);
  g_replay_active = false;
  timing_clock_stop();
  printf("NXRS_REPLAY %s recording_id=%s sequence=%" PRIu32
         " begins=%" PRIu32 " commits=%" PRIu32 " safe_points=%" PRIu32
         " probes=%" PRIu32 " faults=%" PRIu32 " arena_bytes=%u owner=%d\n",
         g_replay_fault == 0 ? "PASS" : "FAIL", g_replay_recording_id,
         g_replay_sequence, g_replay_begin_count, g_replay_commit_count,
         g_replay_safe_count, g_replay_probe_sequence, g_replay_fault,
         ARENA_SIZE, g_replay_owner);
  mm_uninitialize(g_replay_heap);
  g_replay_heap = NULL;
  return g_replay_fault == 0 ? 0 : 1;
}
