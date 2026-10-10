/* SPDX-License-Identifier: MIT
 * Linux-only lifecycle qualification driver for the host test fixture. */
#include "qualification.h"
#include "lifecycle_faults.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <mqueue.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

mqd_t __real_mq_open(const char *, int, ...);
pid_t __real_getpid(void);
int __real_nxrs_cq_thread_start(void **, size_t, void *(*)(void *), void *);

static int queue_fault_enabled;
static unsigned queue_fault_threshold;
static unsigned queue_fault_calls;
static unsigned queue_fault_hits;
static int start_fault_enabled;
static unsigned start_fault_threshold;
static unsigned start_fault_calls;
static unsigned start_fault_hits;
static atomic_int foreign_owner_once;

pid_t __wrap_getpid(void) {
  pid_t pid = __real_getpid();
  return atomic_exchange(&foreign_owner_once, 0) ? pid + 1 : pid;
}

mqd_t __wrap_mq_open(const char *name, int flags, ...) {
  if (queue_fault_enabled) {
    unsigned call = queue_fault_calls++;
    if (call == queue_fault_threshold && queue_fault_hits == 0) {
      queue_fault_hits++;
      errno = ENOSPC;
      return (mqd_t)-1;
    }
  }

  if (flags & O_CREAT) {
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = va_arg(arguments, mode_t);
    struct mq_attr *attributes = va_arg(arguments, struct mq_attr *);
    va_end(arguments);
    return __real_mq_open(name, flags, mode, attributes);
  }
  return __real_mq_open(name, flags);
}

int __wrap_nxrs_cq_thread_start(void **handle, size_t stack,
                                void *(*entry)(void *), void *argument) {
  if (start_fault_enabled) {
    unsigned call = start_fault_calls++;
    if (call == start_fault_threshold && start_fault_hits == 0) {
      start_fault_hits++;
      *handle = NULL;
      return EAGAIN;
    }
  }
  return __real_nxrs_cq_thread_start(handle, stack, entry, argument);
}

struct resources {
  size_t descriptors;
  size_t threads;
  size_t queues;
};

static int count_entries(const char *path, const char *prefix, size_t *count) {
  DIR *directory = opendir(path);
  if (directory == NULL) return -1;
  size_t total = 0;
  struct dirent *entry;
  errno = 0;
  while ((entry = readdir(directory)) != NULL) {
    if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0)
      continue;
    if (prefix == NULL || strncmp(entry->d_name, prefix, strlen(prefix)) == 0)
      total++;
  }
  int read_error = errno;
  int close_error = closedir(directory);
  if (read_error != 0 || close_error != 0) return -1;
  *count = total;
  return 0;
}

static int snapshot(struct resources *result) {
  char prefix[48];
  int length = snprintf(prefix, sizeof(prefix), "sq_%ld_", (long)getpid());
  if (length < 0 || (size_t)length >= sizeof(prefix)) return -1;
  return count_entries("/proc/self/fd", NULL, &result->descriptors) == 0 &&
                 count_entries("/proc/self/task", NULL, &result->threads) == 0 &&
                 count_entries("/dev/mqueue", prefix, &result->queues) == 0
             ? 0
             : -1;
}

static void report_resources(const char *when, const struct resources *actual,
                             const struct resources *baseline) {
  fprintf(stderr,
          "lifecycle_host: %s resource mismatch: fd=%zu (baseline %zu), "
          "threads=%zu (baseline %zu), queues=%zu (expected 0)\n",
          when, actual->descriptors, baseline->descriptors, actual->threads,
          baseline->threads, actual->queues);
}

static int monotonic_milliseconds(long long *value) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return -1;
  *value = (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
  return 0;
}

static void require_baseline(const char *when, const struct resources *baseline) {
  long long start, now;
  if (monotonic_milliseconds(&start) != 0) {
    perror("lifecycle_host: clock_gettime");
    exit(1);
  }
  struct resources actual;
  do {
    if (snapshot(&actual) != 0) {
      fprintf(stderr, "lifecycle_host: cannot snapshot Linux resources (%s)\n", when);
      exit(1);
    }
    if (actual.descriptors != baseline->descriptors || actual.queues != 0) {
      report_resources(when, &actual, baseline);
      exit(1);
    }
    if (actual.threads == baseline->threads) return;
    if (monotonic_milliseconds(&now) != 0) {
      perror("lifecycle_host: clock_gettime");
      exit(1);
    }
    if (now - start >= 1000) break;
    struct timespec pause = {.tv_sec = 0, .tv_nsec = 10000000};
    while (nanosleep(&pause, &pause) != 0 && errno == EINTR) {}
  } while (1);
  report_resources(when, &actual, baseline);
  exit(1);
}

static void require_retained(const char *when,
                             const struct resources *baseline) {
  long long start, now;
  if (monotonic_milliseconds(&start) != 0) exit(1);
  struct resources actual;
  /* Linux may briefly retain an exited task after join returns. Descriptor
   * and queue counts are checked immediately; only that task view may settle. */
  for (;;) {
    if (snapshot(&actual) != 0) {
      fprintf(stderr, "lifecycle_host: cannot snapshot Linux resources (%s)\n", when);
      exit(1);
    }
    if (actual.descriptors != baseline->descriptors + 60 || actual.queues != 60 ||
        actual.threads <= baseline->threads + 1) break;
    if (monotonic_milliseconds(&now) != 0 || now - start >= 1000) break;
    struct timespec pause = {.tv_sec = 0, .tv_nsec = 10000000};
    while (nanosleep(&pause, &pause) != 0 && errno == EINTR) {}
  }
  if (actual.descriptors != baseline->descriptors + 60 || actual.queues != 60 ||
      actual.threads < baseline->threads ||
      actual.threads > baseline->threads + 1) {
    fprintf(stderr,
            "lifecycle_host: %s retained-resource mismatch: fd=%zu (expected %zu), "
            "threads=%zu (baseline %zu, allowed +0..1), queues=%zu (expected 60)\n",
            when, actual.descriptors, baseline->descriptors + 60,
            actual.threads, baseline->threads, actual.queues);
    exit(1);
  }
}

static void run_call(const char *when, unsigned services, unsigned events,
                     int expected, const struct resources *baseline,
                     int retained, int bounded) {
  char service_text[12], event_text[12];
  if (snprintf(service_text, sizeof(service_text), "%u", services) < 0 ||
      snprintf(event_text, sizeof(event_text), "%u", events) < 0) {
    fprintf(stderr, "lifecycle_host: cannot format run arguments\n");
    exit(1);
  }
  char *arguments[] = {"sq", service_text, event_text, "100", NULL};
  long long start = 0, finish = 0;
  if (bounded && monotonic_milliseconds(&start) != 0) {
    perror("lifecycle_host: clock_gettime");
    exit(1);
  }
  int result = nxrs_sq_run(4, arguments);
  if (retained)
    require_retained(when, baseline);
  else
    require_baseline(when, baseline);
  if (bounded && (monotonic_milliseconds(&finish) != 0 || finish - start >= 2000)) {
    fprintf(stderr, "lifecycle_host: %s exceeded 2000ms\n", when);
    exit(1);
  }
  if (result != expected) {
    fprintf(stderr, "lifecycle_host: %s returned %d, expected %d\n", when,
            result, expected);
    exit(1);
  }
}

static unsigned parse_repetitions(const char *text) {
  char *end = NULL;
  errno = 0;
  unsigned long value = strtoul(text, &end, 10);
  if (errno != 0 || end == text || *end != '\0' || value < 1 || value > 100) {
    fprintf(stderr, "usage: lifecycle_host <normal|fail-open|fail-apply|fail-queue|fail-start|fail-stop|fail-join|fail-close> <repetitions 1..100>\n");
    exit(2);
  }
  return (unsigned)value;
}

int main(int argc, char **argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: lifecycle_host <normal|fail-open|fail-apply|fail-queue|fail-start|fail-stop|fail-join|fail-close> <repetitions 1..100>\n");
    return 2;
  }
  const char *scenario = argv[1];
  int valid_scenario = strcmp(scenario, "normal") == 0 ||
                       strcmp(scenario, "fail-open") == 0 ||
                       strcmp(scenario, "fail-apply") == 0 ||
                       strcmp(scenario, "fail-queue") == 0 ||
                       strcmp(scenario, "fail-start") == 0 ||
                       strcmp(scenario, "fail-stop") == 0 ||
                       strcmp(scenario, "fail-join") == 0 ||
                       strcmp(scenario, "fail-close") == 0;
  if (!valid_scenario) {
    fprintf(stderr, "lifecycle_host: unknown scenario: %s\n", scenario);
    return 2;
  }
  unsigned repetitions = parse_repetitions(argv[2]);

  (void)unsetenv("SQ_FAIL_LED_OPEN");
  (void)unsetenv("SQ_FAIL_LED_APPLY");
  struct resources baseline;
  if (snapshot(&baseline) != 0) {
    fprintf(stderr, "lifecycle_host: cannot snapshot Linux resources before first call\n");
    return 1;
  }
  if (baseline.queues != 0) {
    fprintf(stderr, "lifecycle_host: initial owned queue count is %zu, expected 0\n",
            baseline.queues);
    return 1;
  }

  unsigned calls = 0, failures = 0, recoveries = 0;
  if (strcmp(scenario, "normal") == 0) {
    for (unsigned repetition = 0; repetition < repetitions; repetition++) {
      run_call("normal-3", 3, 300, 0, &baseline, 0, 0);
      calls++;
      run_call("normal-20", 20, 300, 0, &baseline, 0, 0);
      calls++;
    }
  } else if (strcmp(scenario, "fail-open") == 0 ||
             strcmp(scenario, "fail-apply") == 0) {
    const char *variable = strcmp(scenario, "fail-open") == 0
                               ? "SQ_FAIL_LED_OPEN"
                               : "SQ_FAIL_LED_APPLY";
    for (unsigned repetition = 0; repetition < repetitions; repetition++) {
      if (setenv(variable, "1", 1) != 0) {
        perror("lifecycle_host: setenv");
        return 1;
      }
      run_call("injected HAL failure", 20, 100, 1, &baseline, 0, 0);
      if (unsetenv(variable) != 0) { perror("lifecycle_host: unsetenv"); return 1; }
      calls++;
      failures++;
      run_call("HAL recovery", 20, 100, 0, &baseline, 0, 0);
      calls++;
      recoveries++;
    }
  } else if (strcmp(scenario, "fail-queue") == 0) {
    const unsigned thresholds[] = {0, 1, 30, 59};
    for (unsigned repetition = 0; repetition < repetitions; repetition++) {
      for (size_t index = 0; index < sizeof(thresholds) / sizeof(thresholds[0]); index++) {
        queue_fault_threshold = thresholds[index];
        queue_fault_calls = queue_fault_hits = 0;
        queue_fault_enabled = 1;
        run_call("injected queue-open failure", 20, 100, 1, &baseline, 0, 0);
        queue_fault_enabled = 0;
        if (queue_fault_hits != 1) {
          fprintf(stderr, "lifecycle_host: mq_open injection at %u was not hit exactly once\n",
                  thresholds[index]);
          return 1;
        }
        calls++;
        failures++;
        run_call("queue recovery", 20, 100, 0, &baseline, 0, 0);
        calls++;
        recoveries++;
      }
    }
  } else if (strcmp(scenario, "fail-start") == 0) {
    const unsigned thresholds[] = {0, 1, 7, 19};
    for (unsigned repetition = 0; repetition < repetitions; repetition++) {
      for (size_t index = 0; index < sizeof(thresholds) / sizeof(thresholds[0]); index++) {
        start_fault_threshold = thresholds[index];
        start_fault_calls = start_fault_hits = 0;
        start_fault_enabled = 1;
        run_call("injected thread-start failure", 20, 100, 1, &baseline, 0, 0);
        start_fault_enabled = 0;
        if (start_fault_hits != 1) {
          fprintf(stderr, "lifecycle_host: thread-start injection at %u was not hit exactly once\n",
                  thresholds[index]);
          return 1;
        }
        calls++;
        failures++;
        run_call("thread-start recovery", 20, 100, 0, &baseline, 0, 0);
        calls++;
        recoveries++;
      }
    }
  } else if (strcmp(scenario, "fail-stop") == 0 ||
             strcmp(scenario, "fail-join") == 0) {
    const unsigned thresholds[] = {0, 7, 19};
    int stop_profile = strcmp(scenario, "fail-stop") == 0;
    for (unsigned repetition = 0; repetition < repetitions; repetition++) {
      for (size_t index = 0; index < sizeof(thresholds) / sizeof(thresholds[0]); index++) {
        sq_fault_arm(stop_profile ? SQ_FAULT_STOP : SQ_FAULT_JOIN,
                     thresholds[index]);
        run_call(stop_profile ? "injected stop-send failure" :
                                "injected thread-join failure",
                 20, 100, 1, &baseline, 1, 1);
        calls++;
        failures++;
        run_call(stop_profile ? "persistent stop-send failure" :
                                "persistent thread-join failure",
                 20, 100, 1, &baseline, 1, 1);
        calls++;
        failures++;
        unsigned hits = sq_fault_hits();
        if (hits < 2) {
          fprintf(stderr,
                  "lifecycle_host: persistent %s injection at %u hit only %u times\n",
                  stop_profile ? "mq_send" : "thread-join", thresholds[index], hits);
          return 1;
        }
        if (!stop_profile) {
          /* Reject a different task's attempt before touching old handles.
           * Only this owner check sees the substituted PID; snapshots use the
           * real process namespace and confirm all retained resources survive. */
          atomic_store(&foreign_owner_once, 1);
          run_call("foreign coordinator rejected", 20, 100, 1, &baseline, 1, 1);
          calls++;
          failures++;
          if (atomic_load(&foreign_owner_once) != 0 || sq_fault_hits() != hits) {
            fprintf(stderr, "lifecycle_host: foreign coordinator touched old join handles\n");
            return 1;
          }
        }
        sq_fault_disarm();
        run_call(stop_profile ? "stop-send recovery" : "thread-join recovery",
                 20, 100, 0, &baseline, 0, 1);
        calls++;
        recoveries++;
      }
    }
  } else {
    const unsigned thresholds[] = {0, 1, 30, 59};
    for (unsigned repetition = 0; repetition < repetitions; repetition++) {
      for (size_t index = 0; index < sizeof(thresholds) / sizeof(thresholds[0]); index++) {
        sq_fault_arm(SQ_FAULT_CLOSE, thresholds[index]);
        run_call("injected mq_close failure", 20, 100, 1, &baseline, 0, 1);
        sq_fault_disarm();
        if (sq_fault_hits() != 1) {
          fprintf(stderr,
                  "lifecycle_host: mq_close injection at %u was not hit exactly once\n",
                  thresholds[index]);
          return 1;
        }
        calls++;
        failures++;
        run_call("mq_close recovery", 20, 100, 0, &baseline, 0, 1);
        calls++;
        recoveries++;
      }
    }
  }

  printf("SQ_LIFECYCLE scenario=%s repetitions=%u calls=%u failures=%u recoveries=%u fd_delta=0 thread_delta=0 queues_left=0\n",
         scenario, repetitions, calls, failures, recoveries);
  return 0;
}
