/* SPDX-License-Identifier: MIT
 * Shared shutdown fault hooks for Linux and NuttX lifecycle qualification. */
#include "lifecycle_faults.h"

#include "qualification.h"

#include <errno.h>
#include <mqueue.h>
#include <stdatomic.h>

int __real_mq_close(mqd_t);
int __real_mq_send(mqd_t, const char *, size_t, unsigned);
int __real_nxrs_cq_thread_join(void *, void **);
void __real_nxrs_sq_record(struct sq_service *, const struct sq_event *);

static atomic_uint fault_kind;
static atomic_uint fault_position;
static atomic_int fault_enabled;
static atomic_int shutdown_armed;
static atomic_uint stop_calls;
static atomic_uint stop_hits;
static atomic_int stop_descriptor_set;
static atomic_long stop_descriptor;
static atomic_uint join_calls;
static atomic_uint join_hits;
static _Atomic(void *) join_handle;
static atomic_uint close_calls;
static atomic_uint close_hits;
static atomic_uint successful_joins;
static atomic_uint successful_closes;

void sq_fault_arm(unsigned kind, unsigned position) {
  atomic_store_explicit(&fault_enabled, 0, memory_order_release);
  atomic_store_explicit(&stop_calls, 0, memory_order_relaxed);
  atomic_store_explicit(&stop_hits, 0, memory_order_relaxed);
  atomic_store_explicit(&stop_descriptor_set, 0, memory_order_relaxed);
  atomic_store_explicit(&stop_descriptor, -1, memory_order_relaxed);
  atomic_store_explicit(&join_calls, 0, memory_order_relaxed);
  atomic_store_explicit(&join_hits, 0, memory_order_relaxed);
  atomic_store_explicit(&join_handle, NULL, memory_order_relaxed);
  atomic_store_explicit(&close_calls, 0, memory_order_relaxed);
  atomic_store_explicit(&close_hits, 0, memory_order_relaxed);
  atomic_store_explicit(&shutdown_armed, 0, memory_order_release);
  atomic_store_explicit(&fault_position, position, memory_order_relaxed);
  atomic_store_explicit(&fault_kind, kind, memory_order_relaxed);
  atomic_store_explicit(&fault_enabled, 1, memory_order_release);
}

void sq_fault_disarm(void) {
  atomic_store_explicit(&fault_enabled, 0, memory_order_release);
}

unsigned sq_fault_hits(void) {
  switch (atomic_load_explicit(&fault_kind, memory_order_relaxed)) {
    case SQ_FAULT_STOP:
      return atomic_load_explicit(&stop_hits, memory_order_relaxed);
    case SQ_FAULT_JOIN:
      return atomic_load_explicit(&join_hits, memory_order_relaxed);
    case SQ_FAULT_CLOSE:
      return atomic_load_explicit(&close_hits, memory_order_relaxed);
    default:
      return 0;
  }
}

unsigned sq_fault_joined(void) {
  return atomic_load_explicit(&successful_joins, memory_order_relaxed);
}

unsigned sq_fault_closed(void) {
  return atomic_load_explicit(&successful_closes, memory_order_relaxed);
}

void __wrap_nxrs_sq_record(struct sq_service *service,
                           const struct sq_event *event) {
  __real_nxrs_sq_record(service, event);
  atomic_store_explicit(&shutdown_armed, 1, memory_order_release);
}

int __wrap_mq_send(mqd_t descriptor, const char *message, size_t length,
                   unsigned priority) {
  if (atomic_load_explicit(&fault_enabled, memory_order_acquire) &&
      atomic_load_explicit(&fault_kind, memory_order_relaxed) == SQ_FAULT_STOP &&
      atomic_load_explicit(&shutdown_armed, memory_order_acquire) &&
      length == sizeof(struct sq_event) &&
      ((const struct sq_event *)message)->kind == SQ_STOP) {
    if (!atomic_load_explicit(&stop_descriptor_set, memory_order_acquire)) {
      unsigned call = atomic_fetch_add_explicit(&stop_calls, 1,
                                                memory_order_relaxed);
      if (call == atomic_load_explicit(&fault_position, memory_order_relaxed)) {
        long expected = -1;
        if (atomic_compare_exchange_strong_explicit(
                &stop_descriptor, &expected, (long)descriptor,
                memory_order_release, memory_order_relaxed)) {
          atomic_store_explicit(&stop_descriptor_set, 1, memory_order_release);
        }
      }
    }
    if (atomic_load_explicit(&stop_descriptor_set, memory_order_acquire) &&
        atomic_load_explicit(&stop_descriptor, memory_order_relaxed) ==
            (long)descriptor) {
      atomic_fetch_add_explicit(&stop_hits, 1, memory_order_relaxed);
      errno = EIO;
      return -1;
    }
  }
  return __real_mq_send(descriptor, message, length, priority);
}

int __wrap_nxrs_cq_thread_join(void *handle, void **result) {
  if (atomic_load_explicit(&fault_enabled, memory_order_acquire) &&
      atomic_load_explicit(&fault_kind, memory_order_relaxed) == SQ_FAULT_JOIN) {
    void *fault_handle = atomic_load_explicit(&join_handle,
                                              memory_order_acquire);
    if (fault_handle != NULL && handle == fault_handle) {
      atomic_fetch_add_explicit(&join_hits, 1, memory_order_relaxed);
      return EINVAL;
    }
    unsigned call = atomic_fetch_add_explicit(&join_calls, 1,
                                              memory_order_relaxed);
    if (call == atomic_load_explicit(&fault_position, memory_order_relaxed)) {
      void *expected = NULL;
      if (atomic_compare_exchange_strong_explicit(
              &join_handle, &expected, handle, memory_order_release,
              memory_order_relaxed)) {
        atomic_fetch_add_explicit(&join_hits, 1, memory_order_relaxed);
        return EINVAL;
      }
    }
  }
  int status = __real_nxrs_cq_thread_join(handle, result);
  if (status == 0)
    atomic_fetch_add_explicit(&successful_joins, 1, memory_order_relaxed);
  return status;
}

int __wrap_mq_close(mqd_t descriptor) {
  if (atomic_load_explicit(&fault_enabled, memory_order_acquire) &&
      atomic_load_explicit(&fault_kind, memory_order_relaxed) == SQ_FAULT_CLOSE) {
    unsigned call = atomic_fetch_add_explicit(&close_calls, 1,
                                              memory_order_relaxed);
    if (call == atomic_load_explicit(&fault_position, memory_order_relaxed) &&
        atomic_fetch_add_explicit(&close_hits, 1, memory_order_relaxed) == 0) {
      int status = __real_mq_close(descriptor);
      if (status != 0) return status;
      atomic_fetch_add_explicit(&successful_closes, 1, memory_order_relaxed);
      errno = EBADF;
      return -1;
    }
  }
  int status = __real_mq_close(descriptor);
  if (status == 0)
    atomic_fetch_add_explicit(&successful_closes, 1, memory_order_relaxed);
  return status;
}
