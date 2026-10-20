/* SPDX-License-Identifier: MIT */
#include <nuttx/config.h>

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <nuttx/irq.h>
#include <netpacket/bluetooth.h>
#include <nuttx/wireless/bluetooth/bt_ioctl.h>

/* NuttX registers NET_LL_BLUETOOTH controllers as bnep%d, not Linux hci%d. */
#define NXRS_BLE_NAME "bnep0"
#define NXRS_BLE_DATA 64
#define NXRS_BLE_PEERS 32

/* Private fixed-width ABI. Target bt_scanresponse_s and btreq_s stay in C. */
struct nxrs_ble_scan_record
{
  uint8_t address[6];
  uint8_t address_type;
  int8_t rssi;
  uint8_t length;
  uint8_t data[NXRS_BLE_DATA];
};

struct nxrs_ble_peer_entry
{
  uint8_t id;
  uint8_t address[6];
  uint8_t address_type;
};

static int g_peer_fd = -1;
static uint64_t g_peer_session;
static bool g_peer_scanning;
static struct nxrs_ble_peer_entry g_peers[NXRS_BLE_PEERS];

int nxrs_ble_peer_bind(int fd, uint64_t session)
{
  irqstate_t flags;
  int result = 0;

  if (fd < 0 || session == 0)
    {
      return EINVAL;
    }

  flags = enter_critical_section();
  if (g_peer_fd >= 0 && g_peer_fd != fd)
    {
      result = EBUSY;
    }
  else if (g_peer_fd != fd || g_peer_session != session)
    {
      memset(g_peers, 0, sizeof(g_peers));
      g_peer_fd = fd;
      g_peer_session = session;
    }
  leave_critical_section(flags);
  return result;
}

int nxrs_ble_peer_remember(int fd, uint64_t session, uint64_t id,
                           const uint8_t address[6], uint8_t address_type)
{
  irqstate_t flags;
  struct nxrs_ble_peer_entry *entry;
  int result = 0;

  if (address == NULL || id == 0 || id > NXRS_BLE_PEERS)
    {
      return EINVAL;
    }

  flags = enter_critical_section();
  if (g_peer_fd != fd || g_peer_session != session)
    {
      result = EPERM;
    }
  else
    {
      entry = &g_peers[id - 1];
      if (entry->id != 0 &&
          (memcmp(entry->address, address, sizeof(entry->address)) != 0 ||
           entry->address_type != address_type))
        {
          result = EEXIST;
        }
      else
        {
          entry->id = (uint8_t)id;
          memcpy(entry->address, address, sizeof(entry->address));
          entry->address_type = address_type;
        }
    }
  leave_critical_section(flags);
  return result;
}

int nxrs_ble_peer_lookup(uint64_t session, uint64_t id,
                         uint8_t address[6], uint8_t *address_type)
{
  irqstate_t flags;
  int result = 0;

  if (id == 0 || id > NXRS_BLE_PEERS || address == NULL || address_type == NULL)
    {
      return EINVAL;
    }

  flags = enter_critical_section();
  if (g_peer_fd < 0 || g_peer_session != session ||
      g_peers[id - 1].id != id)
    {
      result = ENOENT;
    }
  else if (g_peer_scanning)
    {
      result = EBUSY;
    }
  else
    {
      memcpy(address, g_peers[id - 1].address, sizeof(g_peers[id - 1].address));
      *address_type = g_peers[id - 1].address_type;
    }
  leave_critical_section(flags);
  return result;
}

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
  irqstate_t flags = enter_critical_section();
  if (g_peer_fd == fd) { g_peer_scanning = true; }
  leave_critical_section(flags);
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
  int result;
  memcpy(request.btr_name, NXRS_BLE_NAME, sizeof(NXRS_BLE_NAME));
  result = ioctl_status(fd, SIOCBTSCANSTOP, &request);
  /* No registered scan is also quiet, e.g. start failed before registration.
   * The patched host retains registration until a disable is acknowledged.
   */
  if (result == EALREADY) { result = 0; }
  if (result == 0)
    {
      irqstate_t flags = enter_critical_section();
      if (g_peer_fd == fd) { g_peer_scanning = false; }
      leave_critical_section(flags);
    }
  return result;
}

void nxrs_ble_scan_close(int fd)
{
  if (fd >= 0)
    {
      irqstate_t flags = enter_critical_section();
      if (g_peer_fd == fd)
        {
          g_peer_fd = -1;
          g_peer_session = 0;
          g_peer_scanning = false;
          memset(g_peers, 0, sizeof(g_peers));
        }
      leave_critical_section(flags);
      (void)close(fd);
    }
}
