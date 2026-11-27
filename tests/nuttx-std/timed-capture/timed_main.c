/* Exercise the production trace capture command with uninterrupted slow work.
 * This is a QEMU qualification workload, not a GNSS device or CPU benchmark.
 * One fixture worker must progress while the actual trace command waits.
 * Its fixed oracle is printed only after that command stops and the worker joins.
 */
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/note/notectl_driver.h>
#include <nuttx/note/noteram_driver.h>
#include <nuttx/sched_note.h>

#include <builtin/builtin.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_OPERATIONS 96
#define WORKER_STACK_BYTES 8192
#define WORK_BYTES 256
#define FLOOD_NOTES 2048
#define MAX_RAW_NOTES 4096

int timing_clock_initialize(void);
void timing_clock_stop(void);
void acquisition_trace_event(uint8_t edge, const uint8_t *label, size_t length);

struct operation_s
{
  unsigned int source;
  unsigned int sequence;
  uint64_t due;
  uint64_t begin_before;
  uint64_t begin_after;
  uint64_t end_before;
  uint64_t end_after;
  uint64_t fix_before;
  uint64_t fix_after;
  uint32_t checksum;
};

struct worker_s
{
  struct operation_s operations[MAX_OPERATIONS];
  atomic_bool cancel;
  unsigned int seconds;
  unsigned int count;
  unsigned int flooded;
  unsigned long frequency;
  uint64_t enabled_seen;
  uint64_t disabled_seen;
  pid_t task;
  int control;
  int error;
  bool overflow;
};

static struct worker_s g_worker;

static uint64_t ticks(void)
{
  return (uint64_t)perf_gettime();
}

static int pause_worker(void)
{
  const struct timespec pause = {0, 5000000};
  int result = nanosleep(&pause, NULL);
  return result == 0 || errno == EINTR ? 0 : -1;
}

static int enabled(int fd)
{
  struct note_filter_named_mode_s mode = {0};
  strlcpy(mode.name, "ram", sizeof(mode.name));
  if (ioctl(fd, NOTECTL_GETMODE, (unsigned long)&mode) < 0)
    {
      return -1;
    }
  return (mode.mode.flag & NOTE_FILTER_MODE_FLAG_ENABLE) != 0;
}

static int sparse_mode(int fd)
{
  struct note_filter_named_mode_s mode = {0};
  strlcpy(mode.name, "ram", sizeof(mode.name));
  /* Leave scheduler and IRQ recording off in this sparse-window witness.
   * No lifecycle changes occur during the quiet capture margins.
   */
  mode.mode.flag = NOTE_FILTER_MODE_FLAG_DUMP;
  return ioctl(fd, NOTECTL_SETMODE, (unsigned long)&mode);
}

static void marker(uint8_t edge, const char *label)
{
  irqstate_t flags = up_irq_save();
  acquisition_trace_event(edge, (const uint8_t *)label, strlen(label));
  up_irq_restore(flags);
}

static uint32_t work_checksum(unsigned int source, unsigned int sequence)
{
  /* FNV-1a over a specified 256-byte test payload. Volatile keeps the native
   * operation real; host validators independently calculate its returned value.
   */
  volatile uint32_t checksum = UINT32_C(2166136261);
  for (unsigned int index = 0; index < WORK_BYTES; index++)
    {
      uint8_t byte = (uint8_t)(source * 41 + sequence * 17 + index);
      checksum = (checksum ^ byte) * UINT32_C(16777619);
    }
  return checksum;
}

static void operation(struct worker_s *worker, unsigned int source,
                      unsigned int sequence, uint64_t due)
{
  static const char *const scopes[] =
  {
    "gnss/simulated_1hz/read_parse_deliver",
    "gnss/simulated_2hz/read_parse_deliver"
  };
  static const char *const fixes[] =
  {
    "gnss/simulated_1hz/fix_received",
    "gnss/simulated_2hz/fix_received"
  };
  if (worker->count >= MAX_OPERATIONS)
    {
      worker->error = EOVERFLOW;
      return;
    }
  struct operation_s *row = &worker->operations[worker->count++];
  row->source = source;
  row->sequence = sequence;
  row->due = due;
  row->begin_before = ticks();
  marker('B', scopes[source - 1]);
  row->begin_after = ticks();
  row->checksum = work_checksum(source, sequence);
  row->end_before = ticks();
  marker('E', scopes[source - 1]);
  row->end_after = ticks();
  row->fix_before = ticks();
  marker('I', fixes[source - 1]);
  row->fix_after = ticks();
}

static void *produce(void *argument)
{
  struct worker_s *worker = argument;
  uint64_t awaiting = ticks();
  worker->task = gettid();
  while (!atomic_load(&worker->cancel))
    {
      int active = enabled(worker->control);
      if (active < 0 || ticks() - awaiting > worker->frequency * 2)
        {
          worker->error = active < 0 ? errno : ETIMEDOUT;
          return NULL;
        }
      if (active)
        {
          worker->enabled_seen = ticks();
          break;
        }
      if (pause_worker() < 0)
        {
          worker->error = errno;
          return NULL;
        }
    }

  unsigned int next[2] = {0, 0};
  uint64_t first = worker->enabled_seen + worker->frequency / 5;
  while (!atomic_load(&worker->cancel))
    {
      int active = enabled(worker->control);
      uint64_t now = ticks();
      if (active < 0)
        {
          worker->error = errno;
          return NULL;
        }
      if (!active)
        {
          worker->disabled_seen = now;
          return NULL;
        }
      if (now - worker->enabled_seen >
          (uint64_t)worker->frequency * (worker->seconds + 1))
        {
          worker->error = ETIMEDOUT;
          return NULL;
        }
      for (unsigned int source = 1; source <= 2; source++)
        {
          uint64_t due = first +
            (uint64_t)next[source - 1] * worker->frequency / source;
          if (next[source - 1] < worker->seconds * source && now >= due)
            {
              operation(worker, source, ++next[source - 1], due);
              if (worker->error)
                {
                  return NULL;
                }
            }
        }
      if (worker->overflow && worker->flooded == 0 &&
          now - worker->enabled_seen >= worker->frequency * 7 / 20)
        {
          static const char flood[] =
            "qualification/overflow/0123456789012345678901234567890123456789"
            "0123456789012345678901234567890123456789";
          for (unsigned int index = 0; index < FLOOD_NOTES; index++)
            {
              marker('I', flood);
              worker->flooded++;
            }
        }
      if (pause_worker() < 0)
        {
          worker->error = errno;
          return NULL;
        }
    }
  return NULL;
}

static int raw_notes(void)
{
  uint8_t bytes[256];
  unsigned int mode = NOTERAM_MODE_READ_BINARY;
  unsigned int retained = 0;
  unsigned int aligned = 0;
  uint32_t checksum = UINT32_C(2166136261);
  int fd = open("/dev/note/ram", O_RDONLY);
  if (fd < 0 || ioctl(fd, NOTERAM_SETREADMODE, (unsigned long)&mode) < 0)
    {
      if (fd >= 0)
        {
          close(fd);
        }
      return -1;
    }
  ssize_t length;
  while ((length = read(fd, bytes, sizeof(bytes))) > 0)
    {
      if (retained >= MAX_RAW_NOTES || length != bytes[0] ||
          length < sizeof(struct note_common_s) || length > sizeof(bytes))
        {
          close(fd);
          return -1;
        }
      printf("NXRS_TIMED_RAW index=%u bytes=", retained);
      for (ssize_t index = 0; index < length; index++)
        {
          printf("%02x", bytes[index]);
          checksum = (checksum ^ bytes[index]) * UINT32_C(16777619);
        }
      printf("\n");
      aligned += NOTE_ALIGN(length);
      retained++;
    }
  int status = ioctl(fd, NOTERAM_GETMODE, (unsigned long)&mode);
  close(fd);
  if (length < 0 || status < 0)
    {
      return -1;
    }
  printf("NXRS_TIMED_RAW_END records=%u aligned_bytes=%u checksum=%08" PRIx32
         " mode=%u\n", retained, aligned, checksum, mode);
  return 0;
}

static pid_t wait_command(pid_t command, int *status)
{
  pid_t waited;
  do
    {
      waited = waitpid(command, status, 0);
    }
  while (waited < 0 && errno == EINTR);
  return waited;
}

static int owner_recovery(int control)
{
  char *start[] = {"trace", "start", "30", NULL};
  pid_t owner = exec_builtin("trace", start, NULL);
  uint64_t before = ticks();
  int active = 0;
  while (owner >= 0 && ticks() - before < g_worker.frequency * 2)
    {
      active = enabled(control);
      if (active != 0)
        {
          break;
        }
      if (pause_worker() < 0)
        {
          break;
        }
    }
  if (owner < 0 || active != 1 || kill(owner, SIGKILL) < 0)
    {
      printf("NXRS_TIMED_OWNER_FAILURE stage=kill owner=%d active=%d errno=%d\n",
             (int)owner, active, errno);
      return -1;
    }
  int killed_status = -1;
  /* Pinned NuttX's SIGKILL default calls _exit(EXIT_FAILURE), and its
   * WIFSIGNALED macro is always false. Require that actual native termination
   * status plus the still-enabled recorder: normal trace start completion
   * would instead return success after disabling recording.
   */
  if (wait_command(owner, &killed_status) != owner ||
      !WIFEXITED(killed_status) || WEXITSTATUS(killed_status) != EXIT_FAILURE ||
      enabled(control) != 1)
    {
      printf("NXRS_TIMED_OWNER_FAILURE stage=terminated status=%d enabled=%d\n",
             killed_status, enabled(control));
      return -1;
    }
  /* Forced termination need not disable the recorder. The supported command
   * must recover its dead owner's guard so this explicit stop can complete.
   */
  char *stop[] = {"trace", "stop", NULL};
  pid_t stopper = exec_builtin("trace", stop, NULL);
  int stopped_status = -1;
  if (stopper < 0 || wait_command(stopper, &stopped_status) != stopper ||
      !WIFEXITED(stopped_status) || WEXITSTATUS(stopped_status) != 0 ||
      enabled(control) != 0)
    {
      printf("NXRS_TIMED_OWNER_FAILURE stage=recovery status=%d enabled=%d\n",
             stopped_status, enabled(control));
      return -1;
    }
  printf("NXRS_TIMED_OWNER_RECOVERY owner_pid=%d signal=%d stop_pid=%d"
         " owner_exit_status=1 stop_status=0 recorder_enabled=0\n",
         (int)owner, SIGKILL, (int)stopper);
  return 0;
}

int main(int argc, char *argv[])
{
  if (argc != 4 ||
      (strcmp(argv[1], "2") != 0 && strcmp(argv[1], "10") != 0) ||
      (strcmp(argv[3], "normal") != 0 && strcmp(argv[3], "overflow") != 0))
    {
      printf("NXRS_TIMED_WITNESS FAIL reason=arguments\n");
      return 1;
    }
  memset(&g_worker, 0, sizeof(g_worker));
  atomic_init(&g_worker.cancel, false);
  g_worker.seconds = strtoul(argv[1], NULL, 10);
  g_worker.overflow = strcmp(argv[3], "overflow") == 0;
  g_worker.control = open("/dev/notectl", O_RDONLY);
  if (g_worker.control < 0 || sparse_mode(g_worker.control) < 0 ||
      timing_clock_initialize() < 0)
    {
      printf("NXRS_TIMED_WITNESS FAIL reason=setup\n");
      if (g_worker.control >= 0)
        {
          close(g_worker.control);
        }
      return 1;
    }
  g_worker.frequency = perf_getfreq();
  if (g_worker.seconds == 2 && !g_worker.overflow &&
      owner_recovery(g_worker.control) < 0)
    {
      printf("NXRS_TIMED_WITNESS FAIL reason=killed_owner_recovery\n");
      timing_clock_stop();
      close(g_worker.control);
      return 1;
    }
  pthread_t worker;
  pthread_attr_t attributes;
  bool attributes_ready = pthread_attr_init(&attributes) == 0;
  if (!attributes_ready ||
      pthread_attr_setstacksize(&attributes, WORKER_STACK_BYTES) != 0 ||
      pthread_create(&worker, &attributes, produce, &g_worker) != 0)
    {
      printf("NXRS_TIMED_WITNESS FAIL reason=worker_setup\n");
      if (attributes_ready)
        {
          pthread_attr_destroy(&attributes);
        }
      timing_clock_stop();
      close(g_worker.control);
      return 1;
    }
  pthread_attr_destroy(&attributes);
  char *capture[] = {"trace", "capture", argv[1], argv[2], NULL};
  uint64_t before = ticks();
  pid_t command = exec_builtin("trace", capture, NULL);
  int status = -1;
  pid_t waited = -1;
  if (command >= 0)
    {
      waited = wait_command(command, &status);
    }
  uint64_t after = ticks();
  if (command < 0 || waited != command)
    {
      atomic_store(&g_worker.cancel, true);
    }
  int joined = pthread_join(worker, NULL);
  int active = enabled(g_worker.control);
  close(g_worker.control);
  int code = waited == command && WIFEXITED(status) ? WEXITSTATUS(status) : -1;
  bool passed = command >= 0 && joined == 0 && !g_worker.error && active == 0 &&
                g_worker.count == g_worker.seconds * 3 &&
                (g_worker.overflow ? code > 0 : code == 0);
  printf("NXRS_TIMED_ORACLE schema=1 seconds=%u overflow=%u frequency_hz=%lu"
         " recording_id=%s call_before_ticks=%" PRIu64
         " call_after_ticks=%" PRIu64 " enabled_seen_ticks=%" PRIu64
         " disabled_seen_ticks=%" PRIu64 " worker_task_id=%d"
         " operation_count=%u flood_count=%u worker_error=%d capture_status=%d"
         " worker_stack_bytes=%u work_bytes=%u\n",
         g_worker.seconds, g_worker.overflow, g_worker.frequency, argv[2],
         before, after, g_worker.enabled_seen, g_worker.disabled_seen,
         (int)g_worker.task, g_worker.count, g_worker.flooded, g_worker.error,
         code, WORKER_STACK_BYTES, WORK_BYTES);
  printf("NXRS_TIMED_ABI common_bytes=%zu clock_offset=%zu clock_bytes=%zu"
         " pid_offset=%zu pid_bytes=%zu event_bytes=%zu data_offset=%zu"
         " begin_type=%u end_type=%u mark_type=%u word_bytes=%zu\n",
         sizeof(struct note_common_s), offsetof(struct note_common_s, nc_systime),
         sizeof(((struct note_common_s *)0)->nc_systime),
         offsetof(struct note_common_s, nc_pid), sizeof(pid_t),
         sizeof(struct note_event_s), offsetof(struct note_event_s, nev_data),
         NOTE_DUMP_BEGIN, NOTE_DUMP_END, NOTE_DUMP_MARK, sizeof(uintptr_t));
  for (unsigned int index = 0; index < g_worker.count; index++)
    {
      const struct operation_s *row = &g_worker.operations[index];
      printf("NXRS_TIMED_OPERATION source=%u sequence=%u due_ticks=%" PRIu64
             " begin_before_ticks=%" PRIu64 " begin_after_ticks=%" PRIu64
             " end_before_ticks=%" PRIu64 " end_after_ticks=%" PRIu64
             " fix_before_ticks=%" PRIu64 " fix_after_ticks=%" PRIu64
             " checksum=%08" PRIx32 "\n", row->source, row->sequence,
             row->due, row->begin_before, row->begin_after,
             row->end_before, row->end_after, row->fix_before, row->fix_after,
             row->checksum);
    }
  if (raw_notes() < 0)
    {
      passed = false;
    }
  timing_clock_stop();
  printf("NXRS_TIMED_WITNESS %s\n", passed ? "PASS" : "FAIL");
  return passed ? 0 : 1;
}
