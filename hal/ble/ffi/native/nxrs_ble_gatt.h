/* SPDX-License-Identifier: MIT */
#ifndef NXRS_BLE_GATT_H
#define NXRS_BLE_GATT_H

#include <stdint.h>

#define NXRS_GATT_CAPACITY 64

enum nxrs_gatt_kind
{
  NXRS_GATT_CONNECT = 1,
  NXRS_GATT_DISCOVER,
  NXRS_GATT_READ,
  NXRS_GATT_WRITE,
  NXRS_GATT_DISCONNECT,
  NXRS_GATT_LINK_LOST,
  NXRS_GATT_QUIESCENT,
  NXRS_GATT_LINK_LOST_UNKNOWN_REASON, /* OS does not expose an HCI reason */
  NXRS_GATT_REMOTE_ERROR /* normalized errno; OS omitted the raw ATT code */
};

/* Only fixed-width bridge data crosses FFI, never native Bluetooth layouts. */
struct nxrs_gatt_command
{
  uint64_t operation;
  uint64_t peer_session;
  uint64_t peer_id;
  uint32_t kind;
  uint16_t handle;
  uint16_t length;
  uint8_t service[16]; /* Connect: copied desktop OS identity; Discover: UUID */
  uint8_t characteristic[16];
  uint8_t data[NXRS_GATT_CAPACITY];
};

struct nxrs_gatt_event
{
  uint64_t operation;
  uint32_t kind;
  int32_t error;
  uint16_t handle;
  uint16_t length;
  uint8_t att_error;
  uint8_t reason;
  uint8_t data[NXRS_GATT_CAPACITY];
};

_Static_assert(sizeof(struct nxrs_gatt_command) == 128, "GATT command ABI");
_Static_assert(sizeof(struct nxrs_gatt_event) == 88, "GATT event ABI");

int nxrs_ble_gatt_open(uint64_t session, uint16_t capacity,
                       void (*wake)(void *), void (*release)(void *), void *arg);
int nxrs_ble_gatt_submit(uint64_t session, const struct nxrs_gatt_command *cmd);
void nxrs_ble_gatt_recover(uint64_t session, uint64_t operation);
int nxrs_ble_gatt_poll(uint64_t session, struct nxrs_gatt_event *event);
void nxrs_ble_gatt_close(uint64_t session);

#endif
