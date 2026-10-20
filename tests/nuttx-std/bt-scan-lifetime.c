/* Exercise the real scan functions against an independently tracked controller. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define FAR
#define wlerr(...) ((void)0)
#define wlwarn(...) ((void)0)
#define wlinfo(...) ((void)0)
#define OK 0
#define SIOCBTSCANSTOP 1
#define BT_HOST2LE16(value) ((uint16_t)(value))
#define BT_LE_SCAN_DISABLE 0
#define BT_LE_SCAN_ENABLE 1
#define BT_LE_SCAN_ACTIVE 1
#define BT_LE_SCAN_PASSIVE 0
#define BT_LE_SCAN_FILTER_DUP_ENABLE 1
#define BT_HCI_OP_LE_SET_SCAN_PARAMS 0x200b
#define BT_HCI_OP_LE_SET_SCAN_ENABLE 0x200c
#define BT_CONN_CONNECT_SCAN 1
#define BT_ADDR_LE_ANY NULL

typedef void bt_le_scan_cb_t(void);
static bt_le_scan_cb_t *g_scan_dev_found_cb;
static struct { uint8_t scan_enable, scan_filter; } g_btdev;
struct bt_conn_s { int unused; };
struct bt_hci_cp_le_set_scan_params_s
{
  uint8_t scan_type;
  uint16_t interval, window;
  uint8_t filter_policy, addr_type;
};
struct bt_hci_cp_le_set_scan_enable_s { uint8_t enable, filter_dup; };
struct bt_buf_s { uint8_t *data; uint8_t storage[32]; bool allocated; };
static struct bt_buf_s buffers[4];
static bool controller_scanning, late_enable, late_disable;
static int start_error, stop_error, disable_commands, allocated;
static unsigned fail_allocation;
static bool background;
static struct bt_conn_s pending_connection;

static struct bt_buf_s *bt_hci_cmd_create(unsigned opcode, size_t length)
{
  assert(length <= sizeof(buffers[0].storage));
  if (fail_allocation != 0 && fail_allocation == opcode)
    {
      fail_allocation = 0;
      return NULL;
    }
  for (unsigned i = 0; i < sizeof(buffers) / sizeof(buffers[0]); i++)
    if (!buffers[i].allocated)
      {
        buffers[i].allocated = true;
        buffers[i].data = buffers[i].storage;
        allocated++;
        return &buffers[i];
      }
  assert(false);
  return NULL;
}

static void *bt_buf_extend(struct bt_buf_s *buffer, size_t length)
{
  assert(buffer->allocated && length <= sizeof(buffer->storage));
  return buffer->data;
}

static void bt_buf_release(struct bt_buf_s *buffer)
{
  assert(buffer->allocated);
  buffer->allocated = false;
  allocated--;
}

static int bt_hci_cmd_send(unsigned opcode, struct bt_buf_s *buffer)
{
  assert(opcode == BT_HCI_OP_LE_SET_SCAN_PARAMS);
  bt_buf_release(buffer);
  return 0;
}

static int bt_hci_cmd_send_sync(unsigned opcode, struct bt_buf_s *buffer,
                               struct bt_buf_s **response)
{
  assert(opcode == BT_HCI_OP_LE_SET_SCAN_ENABLE);
  struct bt_hci_cp_le_set_scan_enable_s *command = (void *)buffer->data;
  bool enabled = command->enable == BT_LE_SCAN_ENABLE;
  int error = enabled ? start_error : stop_error;
  if (!enabled) disable_commands++;
  if (!error || (enabled ? late_enable : late_disable)) controller_scanning = enabled;
  bt_buf_release(buffer);
  if (error) return error;
  *response = bt_hci_cmd_create(0, 1);
  (*response)->data[0] = 0;
  return 0;
}

static struct bt_conn_s *bt_conn_lookup_state(const void *address, int state)
{
  assert(address == NULL && state == BT_CONN_CONNECT_SCAN);
  return background ? &pending_connection : NULL;
}

static void bt_conn_release(struct bt_conn_s *connection)
{
  assert(connection == &pending_connection);
}

int bt_le_scan_update(void);
/* FUNCTIONS */

static struct { bool bs_scanning; } g_scanstate;
static int stop_ioctl(void)
{
  int ret = -EINVAL;
  switch (SIOCBTSCANSTOP)
    {
      /* IOCTL CASE */
        break;
      default:
        break;
    }
  return ret;
}

static void callback(void) {}

static void reset(void)
{
  assert(allocated == 0);
  g_scan_dev_found_cb = NULL;
  memset(&g_btdev, 0, sizeof(g_btdev));
  controller_scanning = late_enable = late_disable = background = false;
  start_error = stop_error = disable_commands = 0;
  fail_allocation = 0;
}

static void failed_start_can_have_enabled_controller(void)
{
  reset();
  start_error = -EIO;
  late_enable = true; /* Lost completion is not cancellation of the command. */
  assert(bt_start_scanning(1, callback) == -EIO);
  assert(controller_scanning && g_btdev.scan_enable == BT_LE_SCAN_DISABLE);
  assert(g_scan_dev_found_cb == callback);
  stop_error = -EIO;
  assert(bt_stop_scanning() == -EIO);
  assert(g_scan_dev_found_cb == callback);
  assert(controller_scanning);
  assert(bt_start_scanning(1, callback) == -EALREADY);
  stop_error = start_error = 0;
  assert(bt_stop_scanning() == 0);
  assert(disable_commands == 2 && !controller_scanning);
  assert(g_scan_dev_found_cb == NULL);
  assert(bt_start_scanning(1, callback) == 0);
  assert(controller_scanning);
  assert(bt_stop_scanning() == 0 && !controller_scanning);
}

static void failed_stop_retains_callback_until_a_confirmed_retry(void)
{
  reset();
  assert(bt_start_scanning(1, callback) == 0);
  stop_error = -EIO;
  late_disable = true;
  g_scanstate.bs_scanning = true;
  assert(stop_ioctl() == -EIO);
  assert(g_scanstate.bs_scanning);
  assert(!controller_scanning); /* Hardware may finish even when the waiter fails. */
  assert(g_scan_dev_found_cb == callback);
  assert(bt_start_scanning(1, callback) == -EALREADY);
  stop_error = 0;
  assert(stop_ioctl() == 0);
  assert(!g_scanstate.bs_scanning);
  assert(disable_commands == 2 && !controller_scanning);
  assert(g_scan_dev_found_cb == NULL);
}

static void allocation_failure_and_background_scanning(void)
{
  reset();
  fail_allocation = BT_HCI_OP_LE_SET_SCAN_PARAMS;
  assert(bt_start_scanning(1, callback) == -ENOBUFS);
  assert(bt_stop_scanning() == 0 && disable_commands == 1);
  assert(bt_stop_scanning() == -EALREADY);
  assert(bt_start_scanning(1, callback) == 0);
  background = true;
  assert(bt_stop_scanning() == 0);
  assert(controller_scanning && g_scan_dev_found_cb == NULL);
  /* The pending connection owns this passive scan, not the retired callback. */
  background = false;
  assert(bt_le_scan_update() == 0 && !controller_scanning);
}

int main(void)
{
  failed_start_can_have_enabled_controller();
  failed_stop_retains_callback_until_a_confirmed_retry();
  allocation_failure_and_background_scanning();
  assert(allocated == 0);
  return 0;
}
