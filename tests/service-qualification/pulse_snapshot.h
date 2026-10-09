/* SPDX-License-Identifier: MIT
 * Task-context GPIO fixture metadata, not an ISR mailbox. Serialize the GPIO
 * operation with its event copy so acknowledgement cannot race publication.
 */
#ifndef NXRS_SQ_PULSE_SNAPSHOT_H
#define NXRS_SQ_PULSE_SNAPSHOT_H
#include "qualification.h"
#include <pthread.h>

struct sq_pulse_snapshot {
  pthread_mutex_t mutex;
  struct sq_event event;
};
#define SQ_PULSE_SNAPSHOT_INITIALIZER {PTHREAD_MUTEX_INITIALIZER, {0}}

/* Operations are short native GPIO calls. They must not re-enter this object,
 * wait for a service, or run in interrupt context. Return zero on success. */
typedef int (*sq_pulse_operation)(void *);

static inline int sq_pulse_publish(struct sq_pulse_snapshot *pulse,
                                  const struct sq_event *event,
                                  sq_pulse_operation trigger, void *context) {
  if (pthread_mutex_lock(&pulse->mutex) != 0) return -1;
  int result = trigger(context);
  if (result == 0) pulse->event = *event;
  if (pthread_mutex_unlock(&pulse->mutex) != 0) return -1;
  return result;
}

static inline int sq_pulse_receive(struct sq_pulse_snapshot *pulse,
                                  struct sq_event *event,
                                  sq_pulse_operation acknowledge, void *context) {
  if (pthread_mutex_lock(&pulse->mutex) != 0) return -1;
  int result = acknowledge(context);
  if (result == 0) *event = pulse->event;
  if (pthread_mutex_unlock(&pulse->mutex) != 0) return -1;
  return result;
}
#endif
