/* SPDX-License-Identifier: MIT */
#ifndef STD_CHANNEL_PERF_NATIVE_H
#define STD_CHANNEL_PERF_NATIVE_H

#include <stdint.h>

/* Fixture-only target-header ABI. Rust owns sequencing and deadlines. */
#define SCP_GATE_BYTES 128
#define SCP_GATE_ALIGN 16

int scp_has_tcb_witness(void);
int scp_current_tid(void);
int scp_schedule_get(int tid, int *policy, int *priority);
int scp_schedule_set(int tid, int policy, int priority);
/* 0 unavailable, 1 WAIT_SEM, 2 READYTORUN, 3 RUNNING, 4 other, 5 absent. */
int scp_thread_state(int tid, int *priority);
int scp_gate_init(void *storage);
int scp_gate_post(void *storage);
int scp_gate_wait_ms(void *storage, unsigned timeout_ms);
int scp_gate_destroy(void *storage);
int scp_monotonic_ns(uint64_t *value);
int scp_monotonic_resolution_ns(uint64_t *value);
/* Request: 0 baseline perf API, 1 explicitly supported native counter.
 * Source: 1 arch perf, 2 arch timer, 3 scheduler ticks, 4 host monotonic,
 *         5 ESP32-S3 CCOUNT, 6 Pico 2 ARM DWT CYCCNT,
 *         7 NuttX monotonic fallback (functional qualification only). */
int scp_counter_info(unsigned request, uint64_t *frequency, unsigned *source,
                     unsigned *native_bits);
uint32_t scp_counter(void);

#endif
