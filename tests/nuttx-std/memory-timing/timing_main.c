/* Native NuttX timing witness, not an NXRS application benchmark.
 * All timestamps and marker records come from the running target. Capture is
 * disabled before UART output. The ordinary invocation has no debugger stops;
 * --checkpoints RECORDING_ID enables a separate stopped-memory qualification.
 */
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/note/notectl_driver.h>
#include <nuttx/note/noteram_driver.h>
#include <nuttx/sched_note.h>

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define ROUNDS 4
#define MAX_DUMP_BYTES (256 * 1024)
#define CHECKPOINTS 4

/* NuttX timestamps a note before appending it to the RAM ring. Keep each
 * fixture marker atomic against the SysTick IRQ so the single-core stream
 * retains timestamp order. Leave interrupts enabled during the measured work.
 */
#define TIMING_NOTE(record) \
  do \
    { \
      irqstate_t flags = up_irq_save(); \
      record; \
      up_irq_restore(flags); \
    } \
  while (0)

/* This declaration calls the copied production note ABI translator. The labels
 * explicitly identify a fixture: no IMU, UART or sensor interrupt is exercised.
 */
void acquisition_trace_event(uint8_t edge, const uint8_t *label, size_t length);

static void bridge_note(uint8_t edge, const char *label)
{
  size_t length = strlen(label);
  TIMING_NOTE(acquisition_trace_event(edge, (const uint8_t *)label, length));
}

/* Qualification state is fixed storage, read by GDB without target calls.
 * The host supplies a fresh run nonce and independently checks its target copy.
 */
char g_memory_recording_id[33];
volatile unsigned int g_memory_checkpoint_id;
volatile unsigned int g_memory_checkpoint_primask;
void *g_memory_checkpoint_blocks[3];
static bool g_memory_checkpoints;

/* A hardware breakpoint at function entry halts inside the masked section.
 * Keep the empty body addressable even when the fixture is optimized.
 */
void __attribute__((noinline)) timing_checkpoint_stop(void)
{
  __asm__ __volatile__("" ::: "memory");
}

static void memory_checkpoint(unsigned int id)
{
#ifdef CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK
  static const char *const markers[CHECKPOINTS] =
  {
    "memory/checkpoint/1", "memory/checkpoint/2",
    "memory/checkpoint/3", "memory/checkpoint/4"
  };
  if (g_memory_checkpoints)
    {
      irqstate_t flags = enter_critical_section();
      uint8_t primask = getprimask();
      cpsid();
      g_memory_checkpoint_id = id;
      g_memory_checkpoint_primask = getprimask();
      /* The admitted sole note driver writes a fixed RAM ring. It has no
       * active poll subscriber. No allocation, task lifecycle, or interrupt
       * occurs from this marker through the stopped observation. Thus heap
       * geometry and stack reservations are valid at its exact note time.
       */
      sched_note_mark(NOTE_TAG_ALWAYS, markers[id - 1]);
      timing_checkpoint_stop();
      setprimask(primask);
      leave_critical_section(flags);
    }
#else
  (void)id;
#endif
}

static int checkpoint_arguments(int argc, char *argv[])
{
  g_memory_checkpoints = false;
  g_memory_checkpoint_id = 0;
  memset(g_memory_recording_id, 0, sizeof(g_memory_recording_id));
  memset(g_memory_checkpoint_blocks, 0, sizeof(g_memory_checkpoint_blocks));
  if (argc == 1)
    {
      return 0;
    }
#ifdef CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK
  if (argc == 3 && strcmp(argv[1], "--checkpoints") == 0 &&
      strlen(argv[2]) == sizeof(g_memory_recording_id) - 1)
    {
      for (unsigned int i = 0; i < sizeof(g_memory_recording_id) - 1; i++)
        {
          if (!((argv[2][i] >= '0' && argv[2][i] <= '9') ||
                (argv[2][i] >= 'a' && argv[2][i] <= 'f')))
            {
              return -1;
            }
        }
      memcpy(g_memory_recording_id, argv[2], sizeof(g_memory_recording_id));
      g_memory_checkpoints = true;
      return 0;
    }
#else
  (void)argv;
#endif
  return -1;
}

#ifdef CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK
int timing_clock_initialize(void);
void timing_clock_stop(void);
#endif

struct timing_work_s
{
  sem_t request;
  sem_t done;
  int error;
};

static int wait_sem(sem_t *sem)
{
  int result;
  do
    {
      result = sem_wait(sem);
    }
  while (result < 0 && errno == EINTR);
  return result;
}

static int enable_notes(int fd, bool enabled)
{
  /* The pinned driver consumes the named struct, despite the older comment
   * in notectl_driver.h mentioning note_filter_mode_s.
   */
  struct note_filter_named_mode_s mode = {0};
  mode.mode.flag = enabled ? NOTE_FILTER_MODE_FLAG_ENABLE |
                            NOTE_FILTER_MODE_FLAG_SWITCH |
                            NOTE_FILTER_MODE_FLAG_IRQ |
                            NOTE_FILTER_MODE_FLAG_DUMP : 0;
  return ioctl(fd, NOTECTL_SETMODE, (unsigned long)&mode);
}

static void *worker(void *arg)
{
  struct timing_work_s *work = arg;
  unsigned long frequency = perf_getfreq();
  pthread_setname_np(pthread_self(), "timing-worker");
  for (int round = 0; round < ROUNDS; round++)
    {
      if (wait_sem(&work->request) < 0)
        {
          work->error = errno;
          sem_post(&work->done);
          return NULL;
        }

      bridge_note('I', "imu/qemu-fixture/wake");
      TIMING_NOTE(sched_note_beginex(NOTE_TAG_ALWAYS, "timing/worker"));
      bridge_note('B', "imu/qemu-fixture/drain");
      TIMING_NOTE(sched_note_beginex(NOTE_TAG_ALWAYS, "timing/busy"));
      clock_t start = perf_gettime();
      while (perf_gettime() - start < frequency / 500)
        {
          /* Two milliseconds on the declared target clock; no CPU benchmark. */
        }
      TIMING_NOTE(sched_note_endex(NOTE_TAG_ALWAYS, "timing/busy"));
      bridge_note('E', "imu/qemu-fixture/drain");
      TIMING_NOTE(sched_note_beginex(NOTE_TAG_ALWAYS, "timing/sleep"));
      usleep(3000);
      TIMING_NOTE(sched_note_endex(NOTE_TAG_ALWAYS, "timing/sleep"));
      TIMING_NOTE(sched_note_counter(NOTE_TAG_ALWAYS, "round", round + 1));
      TIMING_NOTE(sched_note_endex(NOTE_TAG_ALWAYS, "timing/worker"));
      if (sem_post(&work->done) < 0)
        {
          work->error = errno;
          return NULL;
        }
    }
  return NULL;
}

int main(int argc, char *argv[])
{
  int control = -1;
  int ram = -1;
  unsigned int mode = 99;
  unsigned int end_mode = 99;
  int result = 1;
  struct timing_work_s work = {0};
  pthread_t thread;
  pthread_attr_t attributes;
  char buffer[1024];
  ssize_t received = 0;
  size_t dumped = 0;
  bool complete;
  bool request_ready = false;
  bool done_ready = false;
  bool attributes_ready = false;
  bool thread_started = false;
  bool clock_started = false;
  clock_t before;
  clock_t after;
  unsigned long frequency;

  if (checkpoint_arguments(argc, argv) < 0)
    {
      printf("NXRS_TIMING_WITNESS FAIL reason=invalid_checkpoint_arguments\n");
      return 1;
    }
  control = open("/dev/notectl", O_RDONLY);
  ram = open("/dev/note/ram", O_RDONLY);
  if (control < 0 || ram < 0 || enable_notes(control, false) < 0)
    {
      goto failed;
    }

#ifdef CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK
  if (timing_clock_initialize() < 0)
    {
      goto failed;
    }
  clock_started = true;
#endif
  frequency = perf_getfreq();
  before = perf_gettime();
  usleep(10000);
  after = perf_gettime();
  printf("NXRS_TIMING_CLOCK frequency_hz=%lu before=%" PRIu64
         " after=%" PRIu64 "\n", frequency, (uint64_t)before,
         (uint64_t)after);
  if (frequency == 0 || after <= before)
    {
      printf("NXRS_TIMING_WITNESS FAIL reason=nonadvancing_target_perf_clock\n");
      goto failed;
    }

  request_ready = sem_init(&work.request, 0, 0) == 0;
  done_ready = sem_init(&work.done, 0, 0) == 0;
  attributes_ready = pthread_attr_init(&attributes) == 0;
  if (!request_ready || !done_ready || !attributes_ready ||
      pthread_attr_setstacksize(&attributes, 8192) != 0 ||
      ioctl(ram, NOTERAM_CLEAR, 0ul) < 0 ||
      ioctl(ram, NOTERAM_GETMODE, (unsigned long)&mode) < 0 ||
      mode != NOTERAM_MODE_OVERWRITE_DISABLE)
    {
      goto failed;
    }

  /* This call is after the clear, so a broken disabled filter cannot have its
   * erroneous record erased before the independent host inspects the dump.
   */
  bridge_note('I', "bridge/disabled-before-start");
  if (enable_notes(control, true) < 0)
    {
      goto failed;
    }
  TIMING_NOTE(sched_note_beginex(NOTE_TAG_ALWAYS, "timing/capture"));
  memory_checkpoint(1);
  if (g_memory_checkpoints)
    {
      /* Exceed this fixture's small pre-existing free bins. Qualification
       * still verifies actual neighboring chunk boundaries, not just this
       * request order, before calling the released block an interior hole.
       */
      const size_t sizes[3] = {16384, 32768, 16384};
      for (unsigned int i = 0; i < 3; i++)
        {
          g_memory_checkpoint_blocks[i] = malloc(sizes[i]);
          if (g_memory_checkpoint_blocks[i] == NULL)
            {
              goto failed;
            }
          memset(g_memory_checkpoint_blocks[i], 0xa5, sizes[i]);
        }
    }
  memory_checkpoint(2);
  if (pthread_create(&thread, &attributes, worker, &work) != 0)
    {
      goto failed;
    }
  thread_started = true;
  for (int round = 0; round < ROUNDS; round++)
    {
      TIMING_NOTE(sched_note_beginex(NOTE_TAG_ALWAYS, "timing/dispatch"));
      if (sem_post(&work.request) < 0 || wait_sem(&work.done) < 0 || work.error)
        {
          goto failed;
        }
      TIMING_NOTE(sched_note_endex(NOTE_TAG_ALWAYS, "timing/dispatch"));
      if (round == 0 && g_memory_checkpoints)
        {
          free(g_memory_checkpoint_blocks[1]);
          g_memory_checkpoint_blocks[1] = NULL;
          memory_checkpoint(3);
        }
    }
  if (pthread_join(thread, NULL) != 0)
    {
      goto failed;
    }
  thread_started = false;
  if (g_memory_checkpoints)
    {
      for (unsigned int i = 0; i < 3; i++)
        {
          free(g_memory_checkpoint_blocks[i]);
          g_memory_checkpoint_blocks[i] = NULL;
        }
    }
  memory_checkpoint(4);
  TIMING_NOTE(sched_note_endex(NOTE_TAG_ALWAYS, "timing/capture"));
  if (perf_gettime() >= (clock_t)frequency * 5)
    {
      printf("NXRS_TIMING_WITNESS FAIL reason=capture_exceeds_five_second_clock_window\n");
      goto failed;
    }
  if (enable_notes(control, false) < 0 ||
      ioctl(ram, NOTERAM_GETMODE, (unsigned long)&mode) < 0)
    {
      goto failed;
    }

  bridge_note('I', "bridge/disabled-after-stop");
  printf("NXRS_NOTE_BEGIN schema=1 source=nuttx-noteram-ascii "
#ifdef CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK
         "clock=qemu-an521-cmsdk-timer1-20mhz clock_bits=32 clock_epoch=fixture-invocation "
#else
         "clock=nuttx-unmodified-perf "
#endif
         "cpus=1 mode=%u notes_lost=%u frequency_hz=%lu rounds=%d",
         mode, mode == NOTERAM_MODE_OVERWRITE_OVERFLOW, frequency, ROUNDS);
  if (g_memory_checkpoints)
    {
      printf(" recording_id=%s capture_mode=stopped-checkpoints checkpoints=%d",
             g_memory_recording_id, CHECKPOINTS);
    }
  printf("\n");
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
  printf("NXRS_NOTE_END schema=1 mode=%u notes_lost=%u complete=%u",
         end_mode, end_mode == NOTERAM_MODE_OVERWRITE_OVERFLOW, complete);
  if (g_memory_checkpoints)
    {
      printf(" recording_id=%s", g_memory_recording_id);
    }
  printf("\n");
  if (!complete || mode != NOTERAM_MODE_OVERWRITE_DISABLE || end_mode != mode)
    {
      goto failed;
    }

  printf("NXRS_ACQUISITION_BRIDGE rounds=%d disabled_probes=2\n", ROUNDS);
  printf("NXRS_TIMING_WITNESS PASS rounds=%d dump_bytes=%zu\n", ROUNDS, dumped);
  result = 0;

failed:
  if (control >= 0)
    {
      enable_notes(control, false);
      close(control);
    }
  if (thread_started)
    {
      pthread_cancel(thread);
      pthread_join(thread, NULL);
    }
  for (unsigned int i = 0; i < 3; i++)
    {
      free(g_memory_checkpoint_blocks[i]);
      g_memory_checkpoint_blocks[i] = NULL;
    }
  if (attributes_ready)
    {
      pthread_attr_destroy(&attributes);
    }
  if (request_ready)
    {
      sem_destroy(&work.request);
    }
  if (done_ready)
    {
      sem_destroy(&work.done);
    }
  if (ram >= 0)
    {
      close(ram);
    }
#ifdef CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK
  if (clock_started)
    {
      timing_clock_stop();
    }
#else
  (void)clock_started;
#endif
  if (result != 0)
    {
      printf("NXRS_TIMING_WITNESS FAIL errno=%d\n", errno);
    }
  return result;
}
