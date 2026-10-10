/* SPDX-License-Identifier: MIT */
#include "pulse_snapshot.h"

#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      fprintf(stderr, "check failed at %s:%d: %s\n", __FILE__, __LINE__,     \
              #condition);                                                    \
      return 1;                                                                \
    }                                                                          \
  } while (0)

static struct sq_event event_for(uint32_t sequence) {
  struct sq_event event = {
      .origin_cycles = sequence ^ UINT32_C(0x9e3779b9),
      .sequence = sequence,
      .value = sequence * UINT32_C(2654435761) + UINT32_C(0x1234567),
      .source = (uint16_t)(sequence * 37u + 11u),
      .kind = (uint8_t)(sequence % 3u),
      .reserved = (uint8_t)(sequence * 17u + 3u),
  };
  return event;
}

static int event_matches(const struct sq_event *actual,
                         const struct sq_event *expected) {
  return actual->origin_cycles == expected->origin_cycles &&
         actual->sequence == expected->sequence &&
         actual->value == expected->value && actual->source == expected->source &&
         actual->kind == expected->kind && actual->reserved == expected->reserved;
}

static int succeed(void *context) {
  if (context != NULL) atomic_fetch_add((atomic_int *)context, 1);
  return 0;
}

static int fail(void *context) {
  if (context != NULL) atomic_fetch_add((atomic_int *)context, 1);
  return -7;
}

struct gate {
  pthread_mutex_t mutex;
  pthread_cond_t condition;
  int entered;
  int open;
};

#define GATE_INITIALIZER                                                      \
  { PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, 0, 0 }

static int gated_success(void *context) {
  struct gate *gate = context;
  pthread_mutex_lock(&gate->mutex);
  gate->entered = 1;
  pthread_cond_broadcast(&gate->condition);
  while (!gate->open) pthread_cond_wait(&gate->condition, &gate->mutex);
  pthread_mutex_unlock(&gate->mutex);
  return 0;
}

static void gate_wait_until_entered(struct gate *gate) {
  pthread_mutex_lock(&gate->mutex);
  while (!gate->entered) pthread_cond_wait(&gate->condition, &gate->mutex);
  pthread_mutex_unlock(&gate->mutex);
}

static void gate_open(struct gate *gate) {
  pthread_mutex_lock(&gate->mutex);
  gate->open = 1;
  pthread_cond_broadcast(&gate->condition);
  pthread_mutex_unlock(&gate->mutex);
}

struct start_barrier {
  pthread_mutex_t mutex;
  pthread_cond_t condition;
  int participants_started;
};

#define START_INITIALIZER                                                    \
  { PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, 0 }

static void announce_start(struct start_barrier *start) {
  pthread_mutex_lock(&start->mutex);
  ++start->participants_started;
  pthread_cond_broadcast(&start->condition);
  pthread_mutex_unlock(&start->mutex);
}

static void wait_for_start(struct start_barrier *start, int participants) {
  pthread_mutex_lock(&start->mutex);
  while (start->participants_started < participants)
    pthread_cond_wait(&start->condition, &start->mutex);
  pthread_mutex_unlock(&start->mutex);
}

struct receive_job {
  struct sq_pulse_snapshot *pulse;
  struct sq_event *event;
  sq_pulse_operation acknowledge;
  void *context;
  struct start_barrier *start;
  int result;
};

static void *receive_thread(void *context) {
  struct receive_job *job = context;
  if (job->start != NULL) announce_start(job->start);
  job->result = sq_pulse_receive(job->pulse, job->event, job->acknowledge,
                                 job->context);
  return NULL;
}

struct publish_job {
  struct sq_pulse_snapshot *pulse;
  const struct sq_event *event;
  sq_pulse_operation trigger;
  void *context;
  struct start_barrier *start;
  int result;
};

static void *publish_thread(void *context) {
  struct publish_job *job = context;
  announce_start(job->start);
  job->result = sq_pulse_publish(job->pulse, job->event, job->trigger,
                                 job->context);
  return NULL;
}

static int test_known_event_and_errors(void) {
  struct sq_pulse_snapshot pulse = SQ_PULSE_SNAPSHOT_INITIALIZER;
  const struct sq_event expected = {
      .origin_cycles = UINT32_C(0x89abcdef),
      .sequence = UINT32_C(0x10203040),
      .value = UINT32_C(0xfedcba98),
      .source = UINT16_C(0x7654),
      .kind = UINT8_C(2),
      .reserved = UINT8_C(0xa5),
  };
  const struct sq_event rejected = event_for(23);
  struct sq_event received = {0};
  unsigned char sentinel[sizeof(received)];
  atomic_int calls = 0;

  CHECK(sq_pulse_publish(&pulse, &expected, succeed, &calls) == 0);
  CHECK(sq_pulse_receive(&pulse, &received, succeed, &calls) == 0);
  CHECK(event_matches(&received, &expected));

  CHECK(sq_pulse_publish(&pulse, &rejected, fail, &calls) == -7);
  received = (struct sq_event){0};
  CHECK(sq_pulse_receive(&pulse, &received, succeed, &calls) == 0);
  CHECK(event_matches(&received, &expected));

  memset(&received, 0x5a, sizeof(received));
  memcpy(sentinel, &received, sizeof(received));
  CHECK(sq_pulse_receive(&pulse, &received, fail, &calls) == -7);
  CHECK(memcmp(&received, sentinel, sizeof(received)) == 0);

  received = (struct sq_event){0};
  CHECK(sq_pulse_receive(&pulse, &received, succeed, &calls) == 0);
  CHECK(event_matches(&received, &expected));
  CHECK(atomic_load(&calls) == 6);
  CHECK(pthread_mutex_destroy(&pulse.mutex) == 0);
  return 0;
}

static int test_publish_waits_for_active_receiver(void) {
  struct sq_pulse_snapshot pulse = SQ_PULSE_SNAPSHOT_INITIALIZER;
  const struct sq_event before = event_for(31);
  const struct sq_event after = event_for(32);
  struct sq_event received = {0};
  struct gate gate = GATE_INITIALIZER;
  struct start_barrier start = START_INITIALIZER;
  atomic_int trigger_calls = 0;
  struct receive_job reader = {&pulse, &received, gated_success, &gate, NULL, -99};
  struct publish_job writer = {&pulse, &after, succeed, &trigger_calls, &start, -99};
  pthread_t receiver_thread;
  pthread_t publisher_thread;

  CHECK(sq_pulse_publish(&pulse, &before, succeed, NULL) == 0);
  CHECK(pthread_create(&receiver_thread, NULL, receive_thread, &reader) == 0);
  gate_wait_until_entered(&gate);
  CHECK(pthread_mutex_trylock(&pulse.mutex) == EBUSY);
  CHECK(pthread_create(&publisher_thread, NULL, publish_thread, &writer) == 0);
  wait_for_start(&start, 1);
  CHECK(atomic_load(&trigger_calls) == 0);
  gate_open(&gate);
  CHECK(pthread_join(receiver_thread, NULL) == 0);
  CHECK(pthread_join(publisher_thread, NULL) == 0);
  CHECK(reader.result == 0 && event_matches(&received, &before));
  CHECK(writer.result == 0 && atomic_load(&trigger_calls) == 1);
  received = (struct sq_event){0};
  CHECK(sq_pulse_receive(&pulse, &received, succeed, NULL) == 0);
  CHECK(event_matches(&received, &after));
  CHECK(pthread_mutex_destroy(&pulse.mutex) == 0);
  CHECK(pthread_mutex_destroy(&gate.mutex) == 0);
  CHECK(pthread_cond_destroy(&gate.condition) == 0);
  CHECK(pthread_mutex_destroy(&start.mutex) == 0);
  CHECK(pthread_cond_destroy(&start.condition) == 0);
  return 0;
}

static int test_receive_waits_for_active_publisher(void) {
  struct sq_pulse_snapshot pulse = SQ_PULSE_SNAPSHOT_INITIALIZER;
  const struct sq_event before = event_for(41);
  const struct sq_event after = event_for(42);
  struct sq_event received;
  memset(&received, 0x3c, sizeof(received));
  struct gate gate = GATE_INITIALIZER;
  struct start_barrier start = START_INITIALIZER;
  atomic_int acknowledge_calls = 0;
  struct publish_job writer = {&pulse, &after, gated_success, &gate, &start, -99};
  struct receive_job reader = {&pulse, &received, succeed, &acknowledge_calls,
                               &start, -99};
  pthread_t publisher_thread;
  pthread_t receiver_thread;

  CHECK(sq_pulse_publish(&pulse, &before, succeed, NULL) == 0);
  CHECK(pthread_create(&publisher_thread, NULL, publish_thread, &writer) == 0);
  gate_wait_until_entered(&gate);
  CHECK(pthread_mutex_trylock(&pulse.mutex) == EBUSY);
  CHECK(pthread_create(&receiver_thread, NULL, receive_thread, &reader) == 0);
  wait_for_start(&start, 2);
  CHECK(atomic_load(&acknowledge_calls) == 0);
  gate_open(&gate);
  CHECK(pthread_join(publisher_thread, NULL) == 0);
  CHECK(pthread_join(receiver_thread, NULL) == 0);
  CHECK(writer.result == 0 && reader.result == 0);
  CHECK(atomic_load(&acknowledge_calls) == 1);
  CHECK(event_matches(&received, &after));
  CHECK(pthread_mutex_destroy(&pulse.mutex) == 0);
  CHECK(pthread_mutex_destroy(&gate.mutex) == 0);
  CHECK(pthread_cond_destroy(&gate.condition) == 0);
  CHECK(pthread_mutex_destroy(&start.mutex) == 0);
  CHECK(pthread_cond_destroy(&start.condition) == 0);
  return 0;
}

enum { STRESS_EVENTS = 50000 };

struct stress_state {
  struct sq_pulse_snapshot pulse;
  pthread_mutex_t mutex;
  pthread_cond_t condition;
  int first_published;
  atomic_int failed;
};

static void *stress_publisher(void *context) {
  struct stress_state *state = context;
  for (uint32_t i = 0; i < STRESS_EVENTS; ++i) {
    struct sq_event event = event_for(i);
    if (sq_pulse_publish(&state->pulse, &event, succeed, NULL) != 0)
      atomic_store(&state->failed, 1);
    if (i == 0) {
      pthread_mutex_lock(&state->mutex);
      state->first_published = 1;
      pthread_cond_broadcast(&state->condition);
      pthread_mutex_unlock(&state->mutex);
    }
  }
  return NULL;
}

static void *stress_receiver(void *context) {
  struct stress_state *state = context;
  pthread_mutex_lock(&state->mutex);
  while (!state->first_published)
    pthread_cond_wait(&state->condition, &state->mutex);
  pthread_mutex_unlock(&state->mutex);
  for (int i = 0; i < STRESS_EVENTS; ++i) {
    struct sq_event received;
    if (sq_pulse_receive(&state->pulse, &received, succeed, NULL) != 0) {
      atomic_store(&state->failed, 1);
      continue;
    }
    const struct sq_event expected = event_for(received.sequence);
    if (!event_matches(&received, &expected)) atomic_store(&state->failed, 1);
  }
  return NULL;
}

static int test_stress_event_integrity(void) {
  struct stress_state state = {
      .pulse = SQ_PULSE_SNAPSHOT_INITIALIZER,
      .mutex = PTHREAD_MUTEX_INITIALIZER,
      .condition = PTHREAD_COND_INITIALIZER,
      .first_published = 0,
      .failed = ATOMIC_VAR_INIT(0),
  };
  pthread_t publisher;
  pthread_t receiver;
  CHECK(pthread_create(&publisher, NULL, stress_publisher, &state) == 0);
  CHECK(pthread_create(&receiver, NULL, stress_receiver, &state) == 0);
  CHECK(pthread_join(publisher, NULL) == 0);
  CHECK(pthread_join(receiver, NULL) == 0);
  CHECK(atomic_load(&state.failed) == 0);
  CHECK(pthread_mutex_destroy(&state.pulse.mutex) == 0);
  CHECK(pthread_mutex_destroy(&state.mutex) == 0);
  CHECK(pthread_cond_destroy(&state.condition) == 0);
  return 0;
}

int main(void) {
  if (test_known_event_and_errors() != 0 ||
      test_publish_waits_for_active_receiver() != 0 ||
      test_receive_waits_for_active_publisher() != 0 ||
      test_stress_event_integrity() != 0)
    return 1;
  puts("pulse snapshot tests: PASS");
  return 0;
}
