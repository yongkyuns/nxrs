/* Native NuttX timing witness, not an NXRS application benchmark.
 * All timestamps and marker records come from the running target. Capture is
 * disabled before UART output; no debugger stop participates in the clock.
 */
#include <nuttx/config.h>
#include <nuttx/clock.h>
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
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define ROUNDS 4
#define MAX_DUMP_BYTES (256 * 1024)

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

      sched_note_beginex(NOTE_TAG_ALWAYS, "timing/worker");
      sched_note_beginex(NOTE_TAG_ALWAYS, "timing/busy");
      clock_t start = perf_gettime();
      while (perf_gettime() - start < frequency / 500)
        {
          /* Two milliseconds on the declared target clock; no CPU benchmark. */
        }
      sched_note_endex(NOTE_TAG_ALWAYS, "timing/busy");
      sched_note_beginex(NOTE_TAG_ALWAYS, "timing/sleep");
      usleep(3000);
      sched_note_endex(NOTE_TAG_ALWAYS, "timing/sleep");
      sched_note_counter(NOTE_TAG_ALWAYS, "round", round + 1);
      sched_note_endex(NOTE_TAG_ALWAYS, "timing/worker");
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

  (void)argc;
  (void)argv;
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
      mode != NOTERAM_MODE_OVERWRITE_DISABLE || enable_notes(control, true) < 0)
    {
      goto failed;
    }

  sched_note_beginex(NOTE_TAG_ALWAYS, "timing/capture");
  if (pthread_create(&thread, &attributes, worker, &work) != 0)
    {
      goto failed;
    }
  thread_started = true;
  for (int round = 0; round < ROUNDS; round++)
    {
      sched_note_beginex(NOTE_TAG_ALWAYS, "timing/dispatch");
      if (sem_post(&work.request) < 0 || wait_sem(&work.done) < 0 || work.error)
        {
          goto failed;
        }
      sched_note_endex(NOTE_TAG_ALWAYS, "timing/dispatch");
    }
  if (pthread_join(thread, NULL) != 0)
    {
      goto failed;
    }
  thread_started = false;
  sched_note_endex(NOTE_TAG_ALWAYS, "timing/capture");
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

  printf("NXRS_NOTE_BEGIN schema=1 source=nuttx-noteram-ascii "
#ifdef CONFIG_EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK
         "clock=qemu-an521-cmsdk-timer1-20mhz clock_bits=32 clock_epoch=fixture-invocation "
#else
         "clock=nuttx-unmodified-perf "
#endif
         "cpus=1 mode=%u notes_lost=%u frequency_hz=%lu rounds=%d\n",
         mode, mode == NOTERAM_MODE_OVERWRITE_OVERFLOW, frequency, ROUNDS);
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
  printf("NXRS_NOTE_END schema=1 mode=%u notes_lost=%u complete=%u\n",
         end_mode, end_mode == NOTERAM_MODE_OVERWRITE_OVERFLOW, complete);
  if (!complete || mode != NOTERAM_MODE_OVERWRITE_DISABLE || end_mode != mode)
    {
      goto failed;
    }

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
