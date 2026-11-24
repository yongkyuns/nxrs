/* Bluetooth SDK declarations, descriptor layouts and callback translations. */
#include <nuttx/config.h>
#include <stdbool.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>
#include <nuttx/wireless/bluetooth/bt_core.h>
#include <nuttx/wireless/bluetooth/bt_gatt.h>
#include <nuttx/wireless/bluetooth/bt_hci.h>
#include <wireless/bluetooth/bt_conn.h>
#include <wireless/bluetooth/bt_hcicore.h>
#include <wireless/bluetooth/bt_att.h>

extern void peripheral_connection_rust(bool connected, uint16_t handle);
extern int peripheral_read_rust(void *data, uint8_t count);
extern bool peripheral_notifications_rust(void);
extern void peripheral_notify_rust(bool enabled);
extern uint8_t peripheral_ingress_limit_rust(uint16_t mtu);
extern int peripheral_write_rust(const void *data, uint8_t count, bool framed);

const unsigned int peripheral_max_connections = CONFIG_BLUETOOTH_MAX_CONN;

static struct bt_uuid_s service = { .type = BT_UUID_128 };
static struct bt_uuid_s input = { .type = BT_UUID_128 };
static struct bt_uuid_s output = { .type = BT_UUID_128 };
static struct bt_uuid_s notify_service = { .type = BT_UUID_128 };
static struct bt_uuid_s notify_input = { .type = BT_UUID_128 };
static struct bt_uuid_s notify_output = { .type = BT_UUID_128 };
static bool notify_registered;

static int receive(struct bt_conn_s *connection, const struct bt_gatt_attr_s *attr,
                   const void *data, uint8_t count, uint16_t offset)
{
  (void)connection;
  return offset == 0 ? peripheral_write_rust(data, count, attr->handle == 8) : -1;
}

static int ingress_limit(struct bt_conn_s *connection,
                         const struct bt_gatt_attr_s *attr, void *data,
                         uint8_t count, uint16_t offset)
{
  uint8_t limit = peripheral_ingress_limit_rust(bt_gatt_get_mtu(connection));
  return bt_gatt_attr_read(connection, attr, data, count, offset,
                           &limit, sizeof(limit));
}

static int transmit(struct bt_conn_s *connection, const struct bt_gatt_attr_s *attr,
                    void *data, uint8_t count, uint16_t offset)
{
  (void)connection;
  (void)attr;
  return offset == 0 ? peripheral_read_rust(data, count) : -1;
}

static void connected(struct bt_conn_s *connection, void *context)
{
  (void)context;
  peripheral_connection_rust(true, connection->handle);
}

static void disconnected(struct bt_conn_s *connection, void *context)
{
  (void)context;
  peripheral_connection_rust(false, connection->handle);
}

static struct bt_conn_cb_s callbacks = {
  .connected = connected,
  .disconnected = disconnected,
};
static struct bt_gatt_chrc_s incoming = {
  .uuid = &input, .value_handle = 3, .properties = BT_GATT_CHRC_WRITE,
};
static struct bt_gatt_chrc_s outgoing = {
  .uuid = &output, .value_handle = 5, .properties = BT_GATT_CHRC_READ,
};
static struct bt_gatt_chrc_s notify_incoming = {
  .uuid = &notify_input, .value_handle = 8,
  .properties = BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE |
                BT_GATT_CHRC_WRITE_WITHOUT_RESP,
};
static struct bt_gatt_chrc_s notify_outgoing = {
  .uuid = &notify_output, .value_handle = 10, .properties = BT_GATT_CHRC_NOTIFY,
};
static const struct bt_uuid_s notify_ccc = {
  .type = BT_UUID_16, .u.u16 = BT_UUID_GATT_CCC,
};

/* This stream uses fresh subscriptions on each unbonded connection. */
static int read_notify(struct bt_conn_s *connection,
                       const struct bt_gatt_attr_s *attr,
                       void *data, uint8_t count, uint16_t offset)
{
  uint16_t value = BT_HOST2LE16(peripheral_notifications_rust() ?
                               BT_GATT_CCC_NOTIFY : 0);
  return bt_gatt_attr_read(connection, attr, data, count, offset,
                           &value, sizeof(value));
}

static int write_notify(struct bt_conn_s *connection,
                        const struct bt_gatt_attr_s *attr,
                        const void *data, uint8_t count, uint16_t offset)
{
  uint16_t value;
  (void)connection;
  (void)attr;
  if (offset != 0 || count != sizeof(value))
    {
      return -EINVAL;
    }

  memcpy(&value, data, sizeof(value));
  value = BT_LE162HOST(value);
  if (value != 0 && value != BT_GATT_CCC_NOTIFY)
    {
      return -EINVAL;
    }

  peripheral_notify_rust(value == BT_GATT_CCC_NOTIFY);
  return count;
}

static const struct bt_gatt_attr_s database[] = {
  BT_GATT_PRIMARY_SERVICE(1, &service),
  BT_GATT_CHARACTERISTIC(2, &incoming),
  BT_GATT_DESCRIPTOR(3, &input, BT_GATT_PERM_WRITE, NULL, receive, NULL),
  BT_GATT_CHARACTERISTIC(4, &outgoing),
  BT_GATT_DESCRIPTOR(5, &output, BT_GATT_PERM_READ, transmit, NULL, NULL),
  BT_GATT_PRIMARY_SERVICE(6, &notify_service),
  BT_GATT_CHARACTERISTIC(7, &notify_incoming),
  BT_GATT_DESCRIPTOR(8, &notify_input, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE,
                     ingress_limit, receive, NULL),
  BT_GATT_CHARACTERISTIC(9, &notify_outgoing),
  BT_GATT_DESCRIPTOR(10, &notify_output, 0, NULL, NULL, NULL),
  BT_GATT_DESCRIPTOR(11, &notify_ccc, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE,
                     read_notify, write_notify, NULL),
};

void peripheral_register(const uint8_t *uuids, bool notifications)
{
  memcpy(service.u.u128, uuids, 16);
  memcpy(input.u.u128, uuids + 16, 16);
  memcpy(output.u.u128, uuids + 32, 16);
  notify_registered = notifications;
  if (notifications)
    {
      memcpy(notify_service.u.u128, uuids + 48, 16);
      memcpy(notify_input.u.u128, uuids + 64, 16);
      memcpy(notify_output.u.u128, uuids + 80, 16);
    }

  bt_conn_cb_register(&callbacks);
  bt_gatt_register(database, notifications ?
                   sizeof(database) / sizeof(database[0]) : 5);
}

int peripheral_start(void)
{
  struct bt_eir_s ad[] = {
    { .len = 2, .type = 0x01, .data = { 0x06 } },
    { .len = 17, .type = 0x07 },
    { .len = 0 },
  };
  struct bt_eir_s response[] = {
    { .len = 0, .type = 0x06 },
    { .len = 0 },
  };
  if (notify_registered)
    {
      ad[1].type = 0x06;
      memcpy(ad[1].data, notify_service.u.u128, 16);
      response[0].len = 17;
      memcpy(response[0].data, service.u.u128, 16);
    }
  else
    {
      memcpy(ad[1].data, service.u.u128, 16);
    }

  return bt_start_advertising(BT_LE_ADV_IND, ad, response);
}

void peripheral_stop(void)
{
  (void)bt_stop_advertising();
}

void peripheral_disconnect(struct bt_conn_s *connection)
{
  (void)bt_conn_disconnect(connection, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
}

int peripheral_notify(struct bt_conn_s *connection, const uint8_t *data,
                      size_t count)
{
  int result = bt_att_try_notify(connection, 10, data, count);
  return result == -EAGAIN ? 0 : result;
}
