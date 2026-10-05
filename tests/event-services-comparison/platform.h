/* SPDX-License-Identifier: MIT */
#ifndef NXRS_EVENT_SERVICES_PLATFORM_H
#define NXRS_EVENT_SERVICES_PLATFORM_H
#include "contract.h"
#ifndef ES_MAILBOX
#define ES_MAILBOX 0
#endif
#define ES_QUEUES_PER_SERVICE (ES_MAILBOX ? 1 : 3)
#define ES_QUEUE_SLOTS (ES_MAILBOX ? 24 : 8)
int es_platform_init(void);
void es_platform_close(void);
/* 0 accepted, 1 full/rejected, -1 platform error. NEVER waits for capacity. */
int es_platform_send(unsigned destination, unsigned kind,
                     const struct es_event *);
/* One wait-any across the service inboxes. 1 event, 0 timeout/interruption, -1
 * error. */
int es_platform_wait(unsigned service, uint32_t timeout_cycles,
                     struct es_event *);
uint32_t es_platform_cycles(void);
/* Simulated peripheral completion: suspend this service, never burn CPU. */
int es_platform_io_wait(uint32_t microseconds);
int es_platform_spawn(void *(*entry)(void *));
int es_platform_arrive(void);
/* Wait until every successfully spawned thread has reached its ready gate. */
int es_platform_wait_ready(void);
/* Wait for every service's ready gate, set *epoch BEFORE releasing all gates.
 */
int es_platform_release(uint32_t *epoch);
int es_platform_join(void);
uint32_t es_platform_heap_used(void);
/* Exact live queue depth, or -1 for invalid input/platform failure. */
int es_platform_queue_depth(unsigned destination, unsigned kind);
unsigned es_platform_queue_peak(void);
void es_platform_resources(void);
const char *es_platform_name(void);
#endif
