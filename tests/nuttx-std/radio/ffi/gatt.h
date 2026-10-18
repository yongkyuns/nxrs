/* SPDX-License-Identifier: MIT */
#ifndef NXRS_TEST_RADIO_GATT_H
#define NXRS_TEST_RADIO_GATT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Returns zero on success and a positive errno value on failure. */
int nxrs_radio_gatt_start(void);
int nxrs_radio_gatt_snapshot(uint32_t *writes, uint32_t *reads,
                             uint32_t *invalid, uint32_t *peer_lost);
/* Event counts/reason and native state are separate snapshots; -1 is unknown. */
int nxrs_radio_gatt_connections(uint32_t *connected, uint32_t *disconnected,
                                uint32_t *native_connected,
                                int32_t *disconnect_reason);
int nxrs_radio_gatt_wait_finish(void);
int nxrs_radio_gatt_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* NXRS_TEST_RADIO_GATT_H */
