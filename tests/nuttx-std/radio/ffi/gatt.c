/* SPDX-License-Identifier: MIT */
#include "gatt.h"

#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <nuttx/irq.h>

#include <sys/ioctl.h>
#include <sys/socket.h>
#include <net/if.h>

#include <netpacket/bluetooth.h>
#include <nuttx/wireless/bluetooth/bt_gatt.h>
#include <nuttx/wireless/bluetooth/bt_ioctl.h>
#include <wireless/bluetooth/bt_conn.h>

#ifdef CONFIG_RP23XX_CYW43439_READ_OBSERVER
void nxrs_radio_spi_capture_report(void);
#else
#define nxrs_radio_spi_capture_report() ((void)0)
#endif

#define NXRS_RADIO_NAME      "bnep0"
#define NXRS_RADIO_ADV_NAME  "nxrs-gatt"
#define NXRS_RADIO_VALUE_LEN 20
#define NXRS_RADIO_EXTENDED_VALUES 3
#define NXRS_RADIO_ROUNDS    100
#define NXRS_RADIO_TIMEOUT_MS 10000

enum nxrs_radio_gatt_state_e
{
  NXRS_RADIO_GATT_STOPPED,
  NXRS_RADIO_GATT_STARTING,
  NXRS_RADIO_GATT_RUNNING,
  NXRS_RADIO_GATT_STOPPING
};

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static enum nxrs_radio_gatt_state_e g_state = NXRS_RADIO_GATT_STOPPED;
static int g_fd = -1;
static uint8_t g_value[NXRS_RADIO_VALUE_LEN];
static uint32_t g_writes;
static uint32_t g_reads;
static uint32_t g_invalid;
static uint32_t g_connections;
static uint32_t g_disconnections;
static int32_t g_disconnect_reason = -1;
static int g_has_value;
static int g_value_read;
static struct bt_conn_s *g_selected_conn;
static int g_peer_lost;
static int g_conn_cb_registered;
static int g_stop_in_progress;

static void nxrs_radio_gatt_connected(struct bt_conn_s *conn, void *arg)
{
  int result;

  (void)conn;
  (void)arg;
  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return;
    }

  g_connections++;
  pthread_mutex_unlock(&g_lock);
}

static void nxrs_radio_gatt_disconnected(struct bt_conn_s *conn, void *arg)
{
  int result;

  (void)arg;
  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return;
    }

  g_disconnections++;
  g_disconnect_reason = conn != NULL ? conn->disconn_reason : -1;
  if (conn != NULL && conn == g_selected_conn)
    {
      g_peer_lost = 1;
    }

  pthread_mutex_unlock(&g_lock);
}

static struct bt_gatt_conn_cb_s g_conn_cb =
{
  .connected = nxrs_radio_gatt_connected,
  .disconnected = nxrs_radio_gatt_disconnected,
  .arg = NULL
};

/* The UUID byte arrays use the Bluetooth little-endian 128-bit encoding. */
/* Immutable fixture schemas: V1 keeps its original three handles. V2 owns
 * the extended values.
 */
static const struct bt_uuid_s g_legacy_service_uuid =
{
  .type = BT_UUID_128,
  .u.u128 =
    {
      0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
      0x00, 0x40, 0x01, 0x00, 0x53, 0x52, 0x58, 0x4e
    }
};

static const struct bt_uuid_s g_service_uuid =
{
  .type = BT_UUID_128,
  .u.u128 =
    {
      0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
      0x00, 0x40, 0x02, 0x00, 0x53, 0x52, 0x58, 0x4e
    }
};

static const struct bt_uuid_s g_characteristic_uuid =
{
  .type = BT_UUID_128,
  .u.u128 =
    {
      0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
      0x00, 0x40, 0x01, 0x00, 0x53, 0x52, 0x58, 0x4e
    }
};

static struct bt_gatt_chrc_s g_legacy_characteristic =
{
  .uuid = &g_characteristic_uuid,
  .value_handle = 3,
  .properties = BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE
};

static struct bt_gatt_chrc_s g_characteristic =
{
  .uuid = &g_characteristic_uuid,
  .value_handle = 6,
  .properties = BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE
};

static const struct bt_uuid_s g_extended_uuids[NXRS_RADIO_EXTENDED_VALUES] =
{
  { .type = BT_UUID_128,
    .u.u128 = { 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
                0x00, 0x40, 0x01, 0x00, 0x53, 0x52, 0x58, 0x4e } },
  { .type = BT_UUID_128,
    .u.u128 = { 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
                0x00, 0x40, 0x01, 0x00, 0x53, 0x52, 0x58, 0x4e } },
  { .type = BT_UUID_128,
    .u.u128 = { 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
                0x00, 0x40, 0x01, 0x00, 0x53, 0x52, 0x58, 0x4e } }
};

static uint16_t g_extended_lengths[NXRS_RADIO_EXTENDED_VALUES] =
{
  44, 64, 65
};

static struct bt_gatt_chrc_s g_extended_characteristics[] =
{
  { .uuid = &g_extended_uuids[0], .value_handle = 8,
    .properties = BT_GATT_CHRC_READ },
  { .uuid = &g_extended_uuids[1], .value_handle = 10,
    .properties = BT_GATT_CHRC_READ },
  { .uuid = &g_extended_uuids[2], .value_handle = 12,
    .properties = BT_GATT_CHRC_READ }
};

static int nxrs_radio_gatt_extended_read(struct bt_conn_s *conn,
                                         const struct bt_gatt_attr_s *attr,
                                         void *buffer, uint8_t length,
                                         uint16_t offset)
{
  const uint16_t *value_length;
  uint16_t available;
  uint8_t copied;
  uint8_t index;
  int result;

  if (attr == NULL || attr->user_data == NULL)
    {
      return -EINVAL;
    }

  value_length = attr->user_data;
  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return -result;
    }

  if (g_state != NXRS_RADIO_GATT_STARTING &&
      g_state != NXRS_RADIO_GATT_RUNNING)
    {
      result = -EACCES;
    }
  else if (conn == NULL || conn != g_selected_conn || g_peer_lost)
    {
      result = -EACCES;
    }
  else if (!g_has_value)
    {
      result = -EAGAIN;
    }
  else if (offset > *value_length || (length != 0 && buffer == NULL))
    {
      result = -EINVAL;
    }
  else
    {
      available = *value_length - offset;
      copied = length < available ? length : (uint8_t)available;
      for (index = 0; index < copied; index++)
        {
          ((uint8_t *)buffer)[index] =
            (uint8_t)(*value_length + offset + index);
        }

      result = copied;
    }

  pthread_mutex_unlock(&g_lock);
  return result;
}

static int nxrs_radio_gatt_value_read(struct bt_conn_s *conn,
                                      const struct bt_gatt_attr_s *attr,
                                      void *buffer, uint8_t length,
                                      uint16_t offset)
{
  int result;

  (void)attr;

  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return -result;
    }

  if ((g_state != NXRS_RADIO_GATT_STARTING &&
       g_state != NXRS_RADIO_GATT_RUNNING) || !g_has_value)
    {
      result = -EAGAIN;
    }
  else if (conn == NULL || conn != g_selected_conn || g_peer_lost)
    {
      result = -EACCES;
    }
  else if (offset > NXRS_RADIO_VALUE_LEN)
    {
      result = -EINVAL;
    }
  else
    {
      uint8_t available = NXRS_RADIO_VALUE_LEN - offset;
      uint8_t copied = length < available ? length : available;

      memcpy(buffer, g_value + offset, copied);
      if (offset == 0 && copied == NXRS_RADIO_VALUE_LEN && !g_value_read)
        {
          g_reads++;
          g_value_read = 1;
        }

      result = copied;
    }

  pthread_mutex_unlock(&g_lock);
  return result;
}

static int nxrs_radio_gatt_value_write(struct bt_conn_s *conn,
                                       const struct bt_gatt_attr_s *attr,
                                       const void *buffer, uint8_t length,
                                       uint16_t offset)
{
  const uint8_t *value = buffer;
  uint32_t sequence;
  uint8_t index;
  int result;

  (void)attr;

  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return -result;
    }

  if (g_state != NXRS_RADIO_GATT_STARTING &&
      g_state != NXRS_RADIO_GATT_RUNNING)
    {
      result = -EACCES;
      goto done;
    }

  if (conn == NULL || g_peer_lost ||
      (g_selected_conn != NULL && conn != g_selected_conn))
    {
      result = -EACCES;
      goto done;
    }

  if (length != NXRS_RADIO_VALUE_LEN || offset != 0 ||
      g_writes >= NXRS_RADIO_ROUNDS ||
      (g_writes != 0 && !g_value_read))
    {
      g_invalid++;
      result = -EINVAL;
      goto done;
    }

  sequence = ((uint32_t)value[0] << 24) |
             ((uint32_t)value[1] << 16) |
             ((uint32_t)value[2] << 8) | value[3];
  if (sequence != g_writes)
    {
      g_invalid++;
      result = -EINVAL;
      goto done;
    }

  for (index = 0; index < NXRS_RADIO_VALUE_LEN - sizeof(sequence); index++)
    {
      if (value[4 + index] != (uint8_t)(sequence + index))
        {
          g_invalid++;
          result = -EINVAL;
          goto done;
        }
    }

  /* Bind this fixture to its first valid writer, not the first stack-wide
   * connection notification.  The pointer is an opaque identity only.
   */
  g_selected_conn = conn;
  memcpy(g_value, value, sizeof(g_value));
  g_writes++;
  g_has_value = 1;
  g_value_read = 0;
  result = length;

done:
  pthread_mutex_unlock(&g_lock);
  return result;
}

static struct bt_gatt_attr_s g_attributes[] =
{
  BT_GATT_PRIMARY_SERVICE(1, &g_legacy_service_uuid),
  BT_GATT_CHARACTERISTIC(2, &g_legacy_characteristic),
  {
    .handle = 3,
    .uuid = &g_characteristic_uuid,
    .read = nxrs_radio_gatt_value_read,
    .write = nxrs_radio_gatt_value_write,
    .user_data = NULL,
    .perm = BT_GATT_PERM_READ | BT_GATT_PERM_WRITE
  },
  BT_GATT_PRIMARY_SERVICE(4, &g_service_uuid),
  BT_GATT_CHARACTERISTIC(5, &g_characteristic),
  {
    .handle = 6,
    .uuid = &g_characteristic_uuid,
    .read = nxrs_radio_gatt_value_read,
    .write = nxrs_radio_gatt_value_write,
    .user_data = NULL,
    .perm = BT_GATT_PERM_READ | BT_GATT_PERM_WRITE
  },
  BT_GATT_CHARACTERISTIC(7, &g_extended_characteristics[0]),
  {
    .handle = 8,
    .uuid = &g_extended_uuids[0],
    .read = nxrs_radio_gatt_extended_read,
    .write = NULL,
    .user_data = &g_extended_lengths[0],
    .perm = BT_GATT_PERM_READ
  },
  BT_GATT_CHARACTERISTIC(9, &g_extended_characteristics[1]),
  {
    .handle = 10,
    .uuid = &g_extended_uuids[1],
    .read = nxrs_radio_gatt_extended_read,
    .write = NULL,
    .user_data = &g_extended_lengths[1],
    .perm = BT_GATT_PERM_READ
  },
  BT_GATT_CHARACTERISTIC(11, &g_extended_characteristics[2]),
  {
    .handle = 12,
    .uuid = &g_extended_uuids[2],
    .read = nxrs_radio_gatt_extended_read,
    .write = NULL,
    .user_data = &g_extended_lengths[2],
    .perm = BT_GATT_PERM_READ
  }
};

static int nxrs_radio_gatt_ioctl(int fd, int command,
                                 struct btreq_s *request)
{
  if (ioctl(fd, command, (unsigned long)(uintptr_t)request) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  return 0;
}

/* HCI stores BD_ADDR least-significant byte first. Match the SDK's deliberate
 * last-octet increment (including wrap), not a 48-bit integer increment. */
static int nxrs_radio_address_matches_sdk(const uint8_t bluetooth[6],
                                         const uint8_t wifi[6])
{
  uint8_t expected[6];
  unsigned int index;

  for (index = 0; index < 6; index++)
    {
      expected[index] = wifi[5 - index];
    }

  expected[0]++;
  return memcmp(bluetooth, expected, sizeof(expected)) == 0;
}

static void nxrs_radio_report_identity(int bluetooth_fd)
{
  /* Read cached interface/controller values only. Never log addresses. */
  static const uint8_t fallback[6] = {0xac, 0x1f, 0x12, 0xa2, 0x43, 0x43};
  struct btreq_s bluetooth = {0};
  struct ifreq wifi = {0};
  int wifi_fd;
  int bluetooth_ok;
  int wifi_ok = 0;
  int matches = 0;
  int default_address = 0;

  memcpy(bluetooth.btr_name, NXRS_RADIO_NAME, sizeof(NXRS_RADIO_NAME));
  bluetooth_ok = nxrs_radio_gatt_ioctl(bluetooth_fd, SIOCGBTINFO,
                                      &bluetooth) == 0;
  wifi_fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (wifi_fd >= 0)
    {
      memcpy(wifi.ifr_name, "wlan0", sizeof("wlan0"));
      wifi_ok = ioctl(wifi_fd, SIOCGIFHWADDR,
                      (unsigned long)(uintptr_t)&wifi) == 0;
      close(wifi_fd);
    }

  if (bluetooth_ok)
    {
      default_address = memcmp(bluetooth.btr_bdaddr.val, fallback, 6) == 0;
      if (wifi_ok)
        {
          matches = nxrs_radio_address_matches_sdk(bluetooth.btr_bdaddr.val,
                         (const uint8_t *)wifi.ifr_hwaddr.sa_data);
        }
    }

  printf("NXRS_RADIO_IDENTITY bt_info=%d wifi_info=%d matches_sdk_policy=%d "
         "default_bt_address=%d\n", bluetooth_ok, wifi_ok, matches,
         default_address);
}

int nxrs_radio_gatt_start(void)
{
  struct bt_eir_s advertisement[3] = {0};
  struct bt_eir_s scan_response[3] = {0};
  struct btreq_s request = {0};
  int fd;
  int result;

  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return result;
    }

  if (g_state != NXRS_RADIO_GATT_STOPPED)
    {
      pthread_mutex_unlock(&g_lock);
      return EBUSY;
    }

  g_state = NXRS_RADIO_GATT_STARTING;
  g_writes = 0;
  g_reads = 0;
  g_invalid = 0;
  g_connections = 0;
  g_disconnections = 0;
  g_disconnect_reason = -1;
  g_has_value = 0;
  g_value_read = 0;
  g_selected_conn = NULL;
  g_peer_lost = 0;
  g_conn_cb_registered = 0;
  g_stop_in_progress = 0;
  memset(g_value, 0, sizeof(g_value));
  pthread_mutex_unlock(&g_lock);

  fd = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_L2CAP);
  if (fd < 0)
    {
      result = errno != 0 ? errno : EIO;
      goto failed;
    }

  nxrs_radio_report_identity(fd);
  nxrs_radio_spi_capture_report();

  /* Test modes select workloads, never a different database. V1's cached
   * range remains valid; V2 is a distinct immutable extended-test service.
   */

  bt_gatt_register(g_attributes, sizeof(g_attributes) /
                                 sizeof(g_attributes[0]));

  result = bt_gatt_conn_cb_register(&g_conn_cb);
  if (result < 0)
    {
      result = -result;
      close(fd);
      goto failed;
    }

  pthread_mutex_lock(&g_lock);
  g_conn_cb_registered = 1;
  pthread_mutex_unlock(&g_lock);

  advertisement[0].len = 2;
  advertisement[0].type = BT_EIR_FLAGS;
  advertisement[0].data[0] = BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR;
  advertisement[1].len = 17;
  advertisement[1].type = BT_EIR_UUID128_SOME;
  memcpy(advertisement[1].data, g_service_uuid.u.u128,
         sizeof(g_service_uuid.u.u128));

  /* Two 128-bit UUIDs cannot fit together in a legacy 31-byte packet.
   * Keep V1 discoverable through the scan response, alongside the name.
   */
  scan_response[0].len = 17;
  scan_response[0].type = BT_EIR_UUID128_SOME;
  memcpy(scan_response[0].data, g_legacy_service_uuid.u.u128,
         sizeof(g_legacy_service_uuid.u.u128));
  scan_response[1].len = sizeof(NXRS_RADIO_ADV_NAME);
  scan_response[1].type = BT_EIR_NAME_COMPLETE;
  memcpy(scan_response[1].data, NXRS_RADIO_ADV_NAME,
         sizeof(NXRS_RADIO_ADV_NAME) - 1);

  memcpy(request.btr_name, NXRS_RADIO_NAME, sizeof(NXRS_RADIO_NAME));
  request.btr_advtype = BT_LE_ADV_IND;
  request.btr_advad = advertisement;
  request.btr_advsd = scan_response;
  result = nxrs_radio_gatt_ioctl(fd, SIOCBTADVSTART, &request);
  if (result != 0)
    {
      close(fd);
      goto failed;
    }

  pthread_mutex_lock(&g_lock);
  g_fd = fd;
  g_state = NXRS_RADIO_GATT_RUNNING;
  pthread_mutex_unlock(&g_lock);
  return 0;

failed:
  if (g_conn_cb_registered)
    {
      int unregister_result = bt_gatt_conn_cb_unregister(&g_conn_cb);

      if (unregister_result < 0)
        {
          fprintf(stderr, "NXRS_RADIO_GATT_CALLBACK_UNREGISTER_FAIL code=%d\n",
                  -unregister_result);
        }
      else
        {
          g_conn_cb_registered = 0;
        }
    }

  pthread_mutex_lock(&g_lock);
  g_state = g_conn_cb_registered ? NXRS_RADIO_GATT_STOPPING :
                                    NXRS_RADIO_GATT_STOPPED;
  pthread_mutex_unlock(&g_lock);
  return result;
}

int nxrs_radio_gatt_snapshot(uint32_t *writes, uint32_t *reads,
                             uint32_t *invalid, uint32_t *peer_lost)
{
  int result;

  if (writes == NULL || reads == NULL || invalid == NULL || peer_lost == NULL)
    {
      return EINVAL;
    }

  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return result;
    }

  *writes = g_writes;
  *reads = g_reads;
  *invalid = g_invalid;
  *peer_lost = (uint32_t)g_peer_lost;
  pthread_mutex_unlock(&g_lock);
  return 0;
}

int nxrs_radio_gatt_connections(uint32_t *connected, uint32_t *disconnected,
                                uint32_t *native_connected,
                                int32_t *disconnect_reason)
{
  struct bt_conn_s *conn;
  irqstate_t flags;
  int result;

  if (connected == NULL || disconnected == NULL || native_connected == NULL ||
      disconnect_reason == NULL)
    {
      return EINVAL;
    }

  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return result;
    }

  *connected = g_connections;
  *disconnected = g_disconnections;
  *disconnect_reason = g_disconnect_reason;
  pthread_mutex_unlock(&g_lock);

  flags = enter_critical_section();
  conn = bt_conn_lookup_state(BT_ADDR_LE_ANY, BT_CONN_CONNECTED);
  leave_critical_section(flags);
  *native_connected = conn != NULL;
  if (conn != NULL)
    {
      bt_conn_release(conn);
    }

  return 0;
}

int nxrs_radio_gatt_wait_finish(void)
{
  static const char expected[] = "done\n";
  struct timespec now;
  int64_t deadline;
  size_t matched = 0;
  int saw_cr = 0;
  int result;

  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  deadline = (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000 +
             NXRS_RADIO_TIMEOUT_MS;

  while (matched < sizeof(expected) - 1)
    {
      struct pollfd input =
      {
        .fd = STDIN_FILENO,
        .events = POLLIN
      };
      int64_t remaining;
      int poll_result;
      char byte;
      ssize_t count;

      if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
        {
          return errno != 0 ? errno : EIO;
        }

      remaining = deadline -
                  ((int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000);
      if (remaining <= 0)
        {
          return ETIMEDOUT;
        }

      poll_result = poll(&input, 1, remaining > INT32_MAX ? INT32_MAX :
                                                          (int)remaining);
      if (poll_result < 0)
        {
          if (errno == EINTR)
            {
              continue;
            }

          return errno != 0 ? errno : EIO;
        }

      if (poll_result == 0)
        {
          return ETIMEDOUT;
        }

      if ((input.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0)
        {
          return EIO;
        }

      if ((input.revents & POLLIN) == 0)
        {
          return EIO;
        }

      count = read(STDIN_FILENO, &byte, 1);
      if (count < 0)
        {
          if (errno == EINTR)
            {
              continue;
            }

          return errno != 0 ? errno : EIO;
        }

      if (count != 1)
        {
          return EIO;
        }

      if (byte == '\r' && matched == sizeof(expected) - 2 && !saw_cr)
        {
          saw_cr = 1;
          continue;
        }

      if (byte != expected[matched])
        {
          return EPROTO;
        }

      matched++;
    }

  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return result;
    }

  if (g_writes != NXRS_RADIO_ROUNDS || g_reads != NXRS_RADIO_ROUNDS ||
      g_invalid != 0)
    {
      result = EPROTO;
    }
  else
    {
      result = 0;
    }

  pthread_mutex_unlock(&g_lock);
  return result;
}

static int nxrs_radio_gatt_wait_disconnect(void)
{
  const struct timespec interval = { .tv_sec = 0, .tv_nsec = 10000000 };
  struct timespec now;
  int64_t deadline;

  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  deadline = (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000 +
             NXRS_RADIO_TIMEOUT_MS;
  for (;;)
    {
      uint32_t connected;
      uint32_t disconnected;
      uint32_t native_connected;
      int32_t reason;
      int result;

      if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
        {
          return errno != 0 ? errno : EIO;
        }

      if ((int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000 >= deadline)
        {
          return ETIMEDOUT;
        }

      result = nxrs_radio_gatt_connections(&connected, &disconnected,
                                           &native_connected, &reason);
      if (result != 0)
        {
          return result;
        }

      if (disconnected > connected)
        {
          return EPROTO;
        }

      /* The disconnect callback precedes the stack's final state change.
       * Keep it registered until both independent observations are quiet.
       */
      if (connected == disconnected && native_connected == 0)
        {
          return 0;
        }

      if (nanosleep(&interval, NULL) < 0 && errno != EINTR)
        {
          return errno != 0 ? errno : EIO;
        }
    }
}

int nxrs_radio_gatt_stop(void)
{
  struct btreq_s request = {0};
  int fd;
  int result;
  int unregister_result = 0;

  result = pthread_mutex_lock(&g_lock);
  if (result != 0)
    {
      return result;
    }

  if (g_state == NXRS_RADIO_GATT_STOPPED)
    {
      pthread_mutex_unlock(&g_lock);
      return 0;
    }

  if (g_state != NXRS_RADIO_GATT_RUNNING &&
      g_state != NXRS_RADIO_GATT_STOPPING)
    {
      pthread_mutex_unlock(&g_lock);
      return EBUSY;
    }

  if (g_stop_in_progress)
    {
      pthread_mutex_unlock(&g_lock);
      return EBUSY;
    }

  g_state = NXRS_RADIO_GATT_STOPPING;
  g_stop_in_progress = 1;
  fd = g_fd;
  pthread_mutex_unlock(&g_lock);

  if (fd >= 0)
    {
      memcpy(request.btr_name, NXRS_RADIO_NAME, sizeof(NXRS_RADIO_NAME));
      result = nxrs_radio_gatt_ioctl(fd, SIOCBTADVSTOP, &request);
    }
  else
    {
      result = 0;
    }

  if (result == 0)
    {
      result = nxrs_radio_gatt_wait_disconnect();
    }

  if (g_conn_cb_registered && result == 0)
    {
      unregister_result = bt_gatt_conn_cb_unregister(&g_conn_cb);
      if (unregister_result < 0)
        {
          unregister_result = -unregister_result;
        }
      else
        {
          g_conn_cb_registered = 0;
        }
    }

  if (fd >= 0 && result == 0)
    {
      close(fd);
      pthread_mutex_lock(&g_lock);
      g_fd = -1;
      pthread_mutex_unlock(&g_lock);
    }

  pthread_mutex_lock(&g_lock);
  if (result == 0 && unregister_result == 0)
    {
      g_fd = -1;
      g_state = NXRS_RADIO_GATT_STOPPED;
    }
  g_stop_in_progress = 0;
  pthread_mutex_unlock(&g_lock);

  if (result != 0 && unregister_result != 0)
    {
      fprintf(stderr, "NXRS_RADIO_GATT_CALLBACK_UNREGISTER_FAIL code=%d\n",
              unregister_result);
    }

  nxrs_radio_spi_capture_report();
  return result != 0 ? result : unregister_result;
}
