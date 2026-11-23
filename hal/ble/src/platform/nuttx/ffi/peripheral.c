/* Bluetooth SDK declarations, descriptor layouts and callback translations. */
#include <nuttx/config.h>
#include <stdbool.h>
#include <errno.h>
#include <sys/param.h>
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
extern void peripheral_notify_rust(bool enabled);
extern int peripheral_write_rust(const void *data, uint8_t count);

const unsigned int peripheral_max_connections = CONFIG_BLUETOOTH_MAX_CONN;

static struct bt_uuid_s service = { .type = BT_UUID_128 };
static struct bt_uuid_s input = { .type = BT_UUID_128 };
static struct bt_uuid_s output = { .type = BT_UUID_128 };

static int receive(struct bt_conn_s *connection, const struct bt_gatt_attr_s *attr,
                   const void *data, uint8_t count, uint16_t offset)
{
  (void)connection;
  (void)attr;
  return offset == 0 ? peripheral_write_rust(data, count) : -1;
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
  .uuid = &output, .value_handle = 5, .properties = BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
};
static struct bt_gatt_ccc_cfg_s notify_config[1];

static void notify_changed(uint16_t value)
{
  peripheral_notify_rust(value == BT_GATT_CCC_NOTIFY);
}

static const struct bt_gatt_attr_s database[] = {
  BT_GATT_PRIMARY_SERVICE(1, &service),
  BT_GATT_CHARACTERISTIC(2, &incoming),
  BT_GATT_DESCRIPTOR(3, &input, BT_GATT_PERM_WRITE, NULL, receive, NULL),
  BT_GATT_CHARACTERISTIC(4, &outgoing),
  BT_GATT_DESCRIPTOR(5, &output, BT_GATT_PERM_READ, transmit, NULL, NULL),
  BT_GATT_CCC(6, 5, notify_config, notify_changed),
};

void peripheral_register(const uint8_t *uuids)
{
  memcpy(service.u.u128, uuids, 16);
  memcpy(input.u.u128, uuids + 16, 16);
  memcpy(output.u.u128, uuids + 32, 16);
  bt_conn_cb_register(&callbacks);
  bt_gatt_register(database, sizeof(database) / sizeof(database[0]));
}

int peripheral_start(void)
{
  struct bt_eir_s ad[] = {
    { .len = 2, .type = 0x01, .data = { 0x06 } },
    { .len = 17, .type = 0x07 },
    { .len = 0 },
  };
  const struct bt_eir_s response[] = { { .len = 0 } };
  memcpy(ad[1].data, service.u.u128, 16);
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
  int result = bt_att_try_notify(connection, 5, data, count);
  return result == -EAGAIN ? 0 : result;
}
