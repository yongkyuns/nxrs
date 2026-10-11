/* SPDX-License-Identifier: MIT */
#include <nuttx/config.h>

#include <errno.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <netpacket/bluetooth.h>
#include <nuttx/wireless/bluetooth/bt_ioctl.h>

/* NuttX registers NET_LL_BLUETOOTH controllers as bnep%d, not Linux hci%d. */
#define NXRS_BLE_NAME "bnep0"
#define NXRS_BLE_DATA 64

/* Private fixed-width ABI. Target bt_scanresponse_s and btreq_s stay in C. */
struct nxrs_ble_scan_record
{
  uint8_t address[6];
  uint8_t address_type;
  int8_t rssi;
  uint8_t length;
  uint8_t data[NXRS_BLE_DATA];
};

static int ioctl_status(int fd, int command, struct btreq_s *request)
{
  if (ioctl(fd, command, (unsigned long)(uintptr_t)request) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  return 0;
}

int nxrs_ble_scan_open(void)
{
  int fd = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_L2CAP);
  return fd < 0 ? -(errno != 0 ? errno : EIO) : fd;
}

/* Verify that this socket's native ioctl path can address the controller. */
int nxrs_ble_scan_check(int fd)
{
  struct btreq_s request = {0};
  memcpy(request.btr_name, NXRS_BLE_NAME, sizeof(NXRS_BLE_NAME));
  return ioctl_status(fd, SIOCGBTINFO, &request);
}

int nxrs_ble_scan_start(int fd)
{
  struct btreq_s request = {0};
  memcpy(request.btr_name, NXRS_BLE_NAME, sizeof(NXRS_BLE_NAME));
  request.btr_dupenable = true;
  return ioctl_status(fd, SIOCBTSCANSTART, &request);
}

int nxrs_ble_scan_get(int fd, struct nxrs_ble_scan_record *output,
                      uint8_t capacity, uint8_t *count)
{
  struct bt_scanresponse_s native[8] = {0};
  struct btreq_s request = {0};
  uint8_t index;
  int result;

  if (output == NULL || count == NULL || capacity == 0 || capacity > 8)
    {
      return EINVAL;
    }

  memcpy(request.btr_name, NXRS_BLE_NAME, sizeof(NXRS_BLE_NAME));
  request.btr_nrsp = capacity;
  request.btr_rsp = native;
  result = ioctl_status(fd, SIOCBTSCANGET, &request);
  if (result != 0)
    {
      return result;
    }

  if (request.btr_nrsp > capacity)
    {
      return EOVERFLOW;
    }

  for (index = 0; index < request.btr_nrsp; index++)
    {
      size_t length = native[index].sr_len;
      if (length > NXRS_BLE_DATA || length > sizeof(native[index].sr_data))
        {
          return EOVERFLOW;
        }

      memcpy(output[index].address, native[index].sr_addr.val,
             sizeof(output[index].address));
      output[index].address_type = native[index].sr_addr.type;
      output[index].rssi = native[index].sr_rssi;
      output[index].length = (uint8_t)length;
      memcpy(output[index].data, native[index].sr_data, length);
    }

  *count = request.btr_nrsp;
  return 0;
}

int nxrs_ble_scan_stop(int fd)
{
  struct btreq_s request = {0};
  memcpy(request.btr_name, NXRS_BLE_NAME, sizeof(NXRS_BLE_NAME));
  return ioctl_status(fd, SIOCBTSCANSTOP, &request);
}

void nxrs_ble_scan_close(int fd)
{
  if (fd >= 0)
    {
      (void)close(fd);
    }
}
