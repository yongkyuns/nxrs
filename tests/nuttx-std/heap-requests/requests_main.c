/* Exercise production allocator request/result notes on a private default
 * heap. The independent rows below read actual call return values, aggregate
 * entry counts and payloads; they are not generated from the note buffer.
 * UART output occurs only with tracing disabled. No debugger stops, new worker,
 * or topology reconstruction is used by this qualification workload.
 */
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/mm/mm.h>
#include <nuttx/note/notectl_driver.h>
#include <nuttx/note/noteram_driver.h>
#include <nuttx/sched_note.h>

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#if defined(CONFIG_SMP) || !defined(CONFIG_MM_DEFAULT_MANAGER) || \
    !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_MM_HEAP_MEMPOOL) || \
    !defined(CONFIG_MM_ALLOC_REQUEST_TRACE)
#  error This witness requires single-core flat default allocator request notes
#endif

#define ARENA_BYTES (64 * 1024)
#define ROW_COUNT 12
#define MAX_DUMP_BYTES (256 * 1024)

enum operation_e
{
  CALL_MALLOC,
  CALL_MEMALIGN,
  CALL_REALLOC
};

struct request_row_s
{
  const char *name;
  enum operation_e operation;
  size_t size;
  size_t alignment;
  uintptr_t old_address;
  uintptr_t result_address;
  uint64_t before_count;
  uint64_t after_count;
  uint32_t before_ticks;
  uint32_t after_ticks;
  bool preserved;
};

uint8_t g_requests_arena[ARENA_BYTES] __attribute__((aligned(64)));
struct mm_heap_s *g_requests_heap;
struct request_row_s g_requests_rows[ROW_COUNT];
char g_requests_recording_id[33];
static unsigned int g_step;

int timing_clock_initialize(void);
void timing_clock_stop(void);

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
  memcpy(g_requests_recording_id, argv[1], 33);
  return true;
}

static int enable_notes(int fd, bool enabled)
{
  struct note_filter_named_mode_s mode = {0};
  mode.mode.flag = enabled ? NOTE_FILTER_MODE_FLAG_ENABLE |
                            NOTE_FILTER_MODE_FLAG_DUMP : 0;
  return ioctl(fd, NOTECTL_SETMODE, (unsigned long)&mode);
}

static void marker(const char *label)
{
  /* Keep just the fixture marker's timestamp and append atomic. Allocator
   * calls and payload checks below execute with their ordinary IRQ state.
   */
  irqstate_t flags = up_irq_save();
  sched_note_mark(NOTE_TAG_ALWAYS, label);
  up_irq_restore(flags);
}

void *__attribute__((noinline)) requests_observed_call(
  unsigned int index, const char *name, enum operation_e operation,
  void *old, size_t size, size_t alignment)
{
  struct request_row_s *row = &g_requests_rows[index];
  void *result;
  row->name = name;
  row->operation = operation;
  row->size = size;
  row->alignment = alignment;
  row->old_address = (uintptr_t)old;
  row->preserved = true;
  row->before_count = mm_alloc_requests(g_requests_heap);
  row->before_ticks = (uint32_t)perf_gettime();
  switch (operation)
    {
      case CALL_MALLOC:
        result = mm_malloc(g_requests_heap, size);
        break;
      case CALL_MEMALIGN:
        result = mm_memalign(g_requests_heap, alignment, size);
        break;
      default:
        result = mm_realloc(g_requests_heap, old, size);
        break;
    }
  row->after_ticks = (uint32_t)perf_gettime();
  row->after_count = mm_alloc_requests(g_requests_heap);
  row->result_address = (uintptr_t)result;
  return result;
}

static bool payload_matches(const void *pointer, size_t size, uint8_t expected)
{
  const volatile uint8_t *bytes = pointer;
  for (size_t index = 0; index < size; index++)
    {
      if (bytes[index] != expected)
        {
          return false;
        }
    }
  return true;
}

static void print_rows(void)
{
  static const char *const operations[] = {"malloc", "memalign", "realloc"};
  for (unsigned int index = 0; index < ROW_COUNT; index++)
    {
      const struct request_row_s *row = &g_requests_rows[index];
      const char *phase = index == 0 ? "disabled_before" :
                          index == ROW_COUNT - 1 ? "disabled_after" : "enabled";
      printf("NXRS_REQUEST_ROW index=%u case=%s phase=%s op=%s size=%zu "
             "align=%zu old=0x%" PRIxPTR " result=0x%" PRIxPTR
             " before_count=%" PRIu64 " after_count=%" PRIu64
             " before_ticks=%" PRIu32 " after_ticks=%" PRIu32
             " payload_preserved=%u\n",
             index, row->name, phase, operations[row->operation], row->size,
             row->alignment, row->old_address, row->result_address,
             row->before_count, row->after_count, row->before_ticks,
             row->after_ticks, row->preserved);
    }
}

#define CHECK(expression) do { if (!(expression)) { goto done; } } while (0)
#define CALL(index, name, operation, old, size, alignment) \
  (g_step = (index), requests_observed_call(index, name, operation, old, size, alignment))

int main(int argc, char *argv[])
{
  int control = -1;
  int ram = -1;
  int status = 1;
  unsigned int mode = 99;
  unsigned int end_mode = 99;
  unsigned long frequency;
  clock_t before;
  clock_t after;
  void *a = NULL;
  void *blocker = NULL;
  void *aligned = NULL;
  void *from_null = NULL;
  void *result;
  uintptr_t old_address;
  bool clock_started = false;
  bool complete;
  char buffer[1024];
  ssize_t received = 0;
  size_t dumped = 0;

  g_step = 0;
  CHECK(arguments(argc, argv));
  memset(g_requests_rows, 0, sizeof(g_requests_rows));
  control = open("/dev/notectl", O_RDONLY);
  ram = open("/dev/note/ram", O_RDONLY);
  CHECK(control >= 0 && ram >= 0 && enable_notes(control, false) == 0);
  CHECK(timing_clock_initialize() == 0);
  clock_started = true;
  frequency = perf_getfreq();
  before = perf_gettime();
  usleep(10000);
  after = perf_gettime();
  printf("NXRS_REQUEST_CLOCK frequency_hz=%lu before=%" PRIu64
         " after=%" PRIu64 "\n", frequency, (uint64_t)before, (uint64_t)after);
  CHECK(frequency == 20000000 && after > before);
  g_requests_heap = mm_initialize("request-isolated", g_requests_arena, ARENA_BYTES);
  CHECK(g_requests_heap != NULL);
  CHECK(ioctl(ram, NOTERAM_CLEAR, 0ul) == 0);
  CHECK(ioctl(ram, NOTERAM_GETMODE, (unsigned long)&mode) == 0 &&
        mode == NOTERAM_MODE_OVERWRITE_DISABLE);

  /* This disabled probe is deliberately after CLEAR: an incorrect runtime
   * filter leaves its unwanted entry or result in the inspected note buffer.
   */
  a = CALL(0, "disabled_before", CALL_MALLOC, NULL, 17, 0);
  CHECK(a != NULL);
  mm_free(g_requests_heap, a);
  a = NULL;
  CHECK(enable_notes(control, true) == 0);
  marker("requests/capture/begin");

  a = CALL(1, "malloc_success", CALL_MALLOC, NULL, 4096, 0);
  CHECK(a != NULL);
  memset(a, 0x5a, 512);
  result = CALL(2, "malloc_failure", CALL_MALLOC, NULL, ARENA_BYTES * 2, 0);
  CHECK(result == NULL);

  old_address = (uintptr_t)a;
  result = CALL(3, "realloc_shrink", CALL_REALLOC, a, 1024, 0);
  if (result != NULL)
    {
      a = result;
    }
  CHECK(result != NULL && (uintptr_t)result == old_address);
  g_requests_rows[3].preserved = payload_matches(a, 512, 0x5a);
  CHECK(g_requests_rows[3].preserved);

  old_address = (uintptr_t)a;
  result = CALL(4, "realloc_grow", CALL_REALLOC, a, 3072, 0);
  if (result != NULL)
    {
      a = result;
    }
  CHECK(result != NULL && (uintptr_t)result == old_address);
  g_requests_rows[4].preserved = payload_matches(a, 512, 0x5a);
  CHECK(g_requests_rows[4].preserved);

  blocker = CALL(5, "malloc_blocker", CALL_MALLOC, NULL, 4096, 0);
  CHECK(blocker != NULL);
  old_address = (uintptr_t)a;
  result = CALL(6, "realloc_move", CALL_REALLOC, a, 8192, 0);
  if (result != NULL)
    {
      a = result;
    }
  CHECK(result != NULL && (uintptr_t)result != old_address);
  g_requests_rows[6].preserved = payload_matches(a, 512, 0x5a);
  CHECK(g_requests_rows[6].preserved);

  result = CALL(7, "realloc_failure", CALL_REALLOC, a, ARENA_BYTES * 2, 0);
  if (result != NULL)
    {
      a = result;
    }
  CHECK(result == NULL);
  g_requests_rows[7].preserved = payload_matches(a, 512, 0x5a);
  CHECK(g_requests_rows[7].preserved);

  aligned = CALL(8, "memalign", CALL_MEMALIGN, NULL, 512, 256);
  CHECK(aligned != NULL && ((uintptr_t)aligned & 255) == 0);
  from_null = CALL(9, "realloc_null", CALL_REALLOC, NULL, 256, 0);
  CHECK(from_null != NULL);
  memset(from_null, 0x3c, 1);
  old_address = (uintptr_t)from_null;
  result = CALL(10, "realloc_zero", CALL_REALLOC, from_null, 0, 0);
  if (result != NULL)
    {
      from_null = result;
    }
  /* The pinned allocator clamps zero to its minimum chunk; it does not free
   * oldmem. Verify its native return value and surviving payload explicitly.
   */
  CHECK(result != NULL && (uintptr_t)result == old_address);
  g_requests_rows[10].preserved = payload_matches(from_null, 1, 0x3c);
  CHECK(g_requests_rows[10].preserved);

  marker("requests/capture/end");
  CHECK(enable_notes(control, false) == 0);
  result = CALL(11, "disabled_after", CALL_MALLOC, NULL, 19, 0);
  CHECK(result != NULL);
  mm_free(g_requests_heap, result);
  CHECK(perf_gettime() < (clock_t)frequency * 5);
  CHECK(ioctl(ram, NOTERAM_GETMODE, (unsigned long)&mode) == 0);

  printf("NXRS_NOTE_BEGIN schema=1 source=nuttx-noteram-ascii "
         "clock=qemu-an521-cmsdk-timer1-20mhz clock_bits=32 "
         "clock_epoch=fixture-invocation cpus=1 mode=%u notes_lost=%u "
         "frequency_hz=%lu recording_id=%s\n",
         mode, mode == NOTERAM_MODE_OVERWRITE_OVERFLOW, frequency,
         g_requests_recording_id);
  while (dumped < MAX_DUMP_BYTES && (received = read(ram, buffer, sizeof(buffer))) > 0)
    {
      if (fwrite(buffer, 1, received, stdout) != (size_t)received)
        {
          received = -1;
          break;
        }
      dumped += received;
    }
  complete = received == 0 && dumped > 0 && dumped < MAX_DUMP_BYTES &&
             ioctl(ram, NOTERAM_GETMODE, (unsigned long)&end_mode) == 0;
  printf("NXRS_NOTE_END schema=1 mode=%u notes_lost=%u complete=%u recording_id=%s\n",
         end_mode, end_mode == NOTERAM_MODE_OVERWRITE_OVERFLOW, complete,
         g_requests_recording_id);
  CHECK(complete && mode == NOTERAM_MODE_OVERWRITE_DISABLE && end_mode == mode);
  print_rows();
  printf("NXRS_HEAP_REQUESTS PASS recording_id=%s heap=0x%" PRIxPTR
         " owner=%d rows=%d enabled_rows=10 disabled_rows=2 dump_bytes=%zu"
         " request_note_bytes=%zu result_note_bytes=%zu fixture_rows_bytes=%zu\n",
         g_requests_recording_id, (uintptr_t)g_requests_heap, gettid(), ROW_COUNT,
         dumped, sizeof(struct note_heap_request_s),
         sizeof(struct note_heap_result_s), sizeof(g_requests_rows));
  status = 0;

done:
  if (control >= 0)
    {
      enable_notes(control, false);
    }
  if (g_requests_heap != NULL)
    {
      mm_free(g_requests_heap, a);
      mm_free(g_requests_heap, blocker);
      mm_free(g_requests_heap, aligned);
      mm_free(g_requests_heap, from_null);
      mm_uninitialize(g_requests_heap);
      g_requests_heap = NULL;
    }
  if (ram >= 0)
    {
      close(ram);
    }
  if (control >= 0)
    {
      close(control);
    }
  if (clock_started)
    {
      timing_clock_stop();
    }
  if (status != 0)
    {
      printf("NXRS_HEAP_REQUESTS FAIL step=%u errno=%d\n", g_step, errno);
    }
  return status;
}
