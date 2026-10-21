/* Target SDK layouts and callback/LPWORK dispatch only. Behavior lives in Rust. */
#include <stdint.h>
#include <stddef.h>
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/wqueue.h>
#include <nuttx/wireless/bluetooth/bt_core.h>
#include <nuttx/wireless/bluetooth/bt_gatt.h>
#include <nuttx/wireless/bluetooth/bt_hci.h>
#include <wireless/bluetooth/bt_conn.h>
#include <wireless/bluetooth/bt_att.h>
#include <errno.h>
#include <string.h>
_Static_assert(CONFIG_SCHED_LPNTHREADS == 1, "GATT requires serialized LPWORK");
#if !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_LIBC_USRWORK)
# error "GATT requires flat-build kernel work queues"
#endif

extern void nxrs_gatt_dispatch_rust(void *);
extern void nxrs_gatt_connected_rust(void *);
extern void nxrs_gatt_disconnected_rust(void *, uint8_t);
extern uint8_t nxrs_gatt_discovered_rust(uint16_t, uint16_t, uint16_t, int);
extern void nxrs_gatt_discovery_done_rust(uint8_t);
extern void nxrs_gatt_written_rust(void *, uint8_t);
extern void nxrs_gatt_read_rust(void *, int, const void *, uint16_t);
static struct work_s dispatch_work;
static struct bt_gatt_conn_cb_s native_callbacks;
static struct bt_gatt_discover_params_s native_discovery;
static struct bt_uuid_s discovery_uuid;
static int characteristic_layout;

uintptr_t nxrs_gatt_lock(void) { return (uintptr_t)enter_critical_section(); }
void nxrs_gatt_unlock(uintptr_t flags) { leave_critical_section((irqstate_t)flags); }
int nxrs_gatt_schedule(uint32_t ms)
{ return work_queue(LPWORK, &dispatch_work, nxrs_gatt_dispatch_rust, NULL, MSEC2TICK(ms)); }
void nxrs_gatt_unschedule(void) { (void)work_cancel(LPWORK, &dispatch_work); }
int nxrs_gatt_error(uint32_t kind)
{
  switch (kind) {
    case 1: return EINVAL; case 2: return EBUSY; case 3: return ENOTCONN;
    case 4: return ENODEV; case 6: return ENOBUFS; case 7: return ENOTSUP;
    case 8: return ENOENT; case 9: return EEXIST; case 10: return EPROTO;
    default: return EIO;
  }
}
static void connected(struct bt_conn_s *conn, void *arg)
{ (void)arg; nxrs_gatt_connected_rust(conn); }
static void disconnected(struct bt_conn_s *conn, void *arg)
{ (void)arg; nxrs_gatt_disconnected_rust(conn, conn->disconn_reason); }
static uint8_t discovered(const struct bt_gatt_attr_s *attr, void *arg)
{
  struct bt_gatt_discover_params_s *params = arg;
  const struct bt_gatt_chrc_s *value = attr->user_data;
  uint8_t again = nxrs_gatt_discovered_rust(attr->handle, params->group_end_handle,
                                           characteristic_layout && value ? value->value_handle : 0, value != NULL);
  return again ? BT_GATT_ITER_CONTINUE : BT_GATT_ITER_STOP;
}
static void discovery_done(void *arg)
{ nxrs_gatt_discovery_done_rust(((struct bt_gatt_discover_params_s *)arg)->result); }
static void written(struct bt_conn_s *conn, uint8_t error)
{ nxrs_gatt_written_rust(conn, error); }
static void read_value(struct bt_conn_s *conn, int error, const void *data, uint16_t length)
{ nxrs_gatt_read_rust(conn, error, data, length); }
int nxrs_gatt_register(void)
{
  native_callbacks.connected = connected; native_callbacks.disconnected = disconnected;
  return bt_gatt_conn_cb_register(&native_callbacks);
}
int nxrs_gatt_unregister(void) { return bt_gatt_conn_cb_unregister(&native_callbacks); }
void *nxrs_gatt_lookup(const uint8_t address[6], uint8_t type)
{
  bt_addr_le_t peer; memcpy(peer.val, address, 6); peer.type = type;
  return bt_conn_lookup_addr_le(&peer);
}
void *nxrs_gatt_create(const uint8_t address[6], uint8_t type)
{
  bt_addr_le_t peer; memcpy(peer.val, address, 6); peer.type = type;
  return bt_conn_create_le(&peer);
}
void nxrs_gatt_release_conn(void *conn) { bt_conn_release(conn); }
int nxrs_gatt_conn_quiet(void *conn)
{ return ((struct bt_conn_s *)conn)->state == BT_CONN_DISCONNECTED; }
void nxrs_gatt_cancel(void *conn) { bt_gatt_cancel(conn); }
int nxrs_gatt_disconnect(void *conn) { return bt_conn_disconnect(conn, 0x13); }
uint16_t nxrs_gatt_mtu(void *conn) { return bt_gatt_get_mtu(conn); }
static void uuid_from_canonical(struct bt_uuid_s *uuid, const uint8_t canonical[16])
{
  static const uint8_t base[12] = {0,0,0x10,0,0x80,0,0,0x80,0x5f,0x9b,0x34,0xfb};
  if (!canonical[0] && !canonical[1] && !memcmp(canonical + 4, base, 12)) {
    (*uuid).type = BT_UUID_16;
    (*uuid).u.u16 = ((uint16_t)canonical[2] << 8) | canonical[3];
  } else {
    (*uuid).type = BT_UUID_128;
    for (unsigned i=0; i<16; i++) (*uuid).u.u128[i] = canonical[15-i];
  }
}

/* The native API retains this static parameter storage until its destroy callback. */
int nxrs_gatt_discover(void *conn, int characteristic, uint16_t start, uint16_t end,
                       const uint8_t canonical[16])
{
  characteristic_layout = characteristic;
  memset(&native_discovery, 0, sizeof(native_discovery));
  uuid_from_canonical(&discovery_uuid, canonical);
  native_discovery.func = discovered; native_discovery.destroy = discovery_done;
  native_discovery.start_handle = start; native_discovery.end_handle = end;
  native_discovery.uuid = &discovery_uuid;
  return characteristic ? bt_gatt_discover_characteristic(conn, &native_discovery) : bt_gatt_discover(conn, &native_discovery);
}
int nxrs_gatt_read(void *conn, uint16_t handle, uint16_t offset)
{ return bt_gatt_read(conn, handle, offset, read_value); }
int nxrs_gatt_write(void *conn, uint16_t handle, const uint8_t *data, uint16_t length)
{ return bt_gatt_write(conn, handle, data, length, written); }
