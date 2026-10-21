/* Production BlueZ bridge over private IPC, never the host Bluetooth adapter.
 * Schemas, caller ownership and callback retirement are tested, not RF quality.
 */
#include <gio/gio.h>
#include <assert.h>
#include <string.h>
#include <sys/wait.h>
#include <stdbool.h>
#include <stdint.h>

static gint clock_offset;
int64_t nxrs_radio_test_clock(int64_t real) {return real+g_atomic_int_get(&clock_offset);}
bool nxrs_radio_test_fast(void) {return false;}
#include "../../hal/ble/ffi/linux/gio.c"
#include "../../hal/ble/ffi/native/nxrs_ble_gatt.h"
#include <errno.h>
#define BLUEZ "org.bluez"
#define ADAPTER BLUEZ ".Adapter1"
#define DEVICE BLUEZ ".Device1"
#define B_DEVICE DEVICE
#define B_SERVICE BLUEZ ".GattService1"
#define B_CHAR BLUEZ ".GattCharacteristic1"
#define PROPERTIES "org.freedesktop.DBus.Properties"
#define OBJECTS "org.freedesktop.DBus.ObjectManager"
#define PENDING_REPORTS 32
#define PATH_BYTES 256
extern void bluez_uuid(const uint8_t *, char *);
extern void bluez_identity(const char *, const char *, uint8_t *);
extern void nxrs_ble_scan_test_pump(void *);
extern size_t nxrs_ble_scan_test_field(void *, unsigned);
extern void nxrs_ble_scan_test_queue(void *, const char *);
extern void nxrs_linux_gatt_test_contend(uint64_t);
extern void nxrs_linux_gatt_test_saturate(void);

#define RADIO "/org/bluez/test_adapter"
#define OTHER_RADIO "/org/bluez/other_adapter"
#define PEER RADIO "/test_peer"
#define SERVICE "4e585253-0001-4000-8000-000000000001"
#define SERVICE_V2 "4e585253-0002-4000-8000-000000000001"
static const guint8 service_uuid[16] = {0x4e, 0x58, 0x52, 0x53, 0, 1, 0x40, 0,
                                      0x80, 0, 0, 0, 0, 0, 0, 1};
static GMutex lock;
static struct {
  gboolean powered, multiple, denied, no_adapter, malformed, wrong_adapter;
  gboolean wrong_uuid, absent_rssi, invalid_rssi, uuid_only, reject_start;
  gboolean hold_start, lost_start_reply, owned;
  guint starts, stops, fail_stops;
  gint16 rssi;
  GDBusMessage *pending;
  char *caller;
  gboolean connected, hold_connect, reject_connect, duplicate_service, duplicate_char;
  const char *read_error;
  guint connects, disconnects, gatt_reads, gatt_writes, deny_disconnects;
  GDBusMessage *gatt_pending;
  guint8 echo[64]; gsize echo_length;
} fixture;

static GVariant *adapter_values(void)
{
  GVariantBuilder values; g_variant_builder_init(&values, G_VARIANT_TYPE_VARDICT);
  if (!fixture.malformed)
    g_variant_builder_add(&values, "{sv}", "Powered", g_variant_new_boolean(fixture.powered));
  return g_variant_builder_end(&values);
}

static GVariant *device_values(void)
{
  GVariantBuilder values; g_variant_builder_init(&values, G_VARIANT_TYPE_VARDICT);
  g_variant_builder_add(&values, "{sv}", "Adapter",
    g_variant_new_object_path(fixture.wrong_adapter ? OTHER_RADIO : RADIO));
  const char *uuids[] = {fixture.wrong_uuid ? "0000180f-0000-1000-8000-00805f9b34fb" : SERVICE,
                         SERVICE_V2};
  g_variant_builder_add(&values, "{sv}", "UUIDs", g_variant_new_strv(uuids, fixture.wrong_uuid ? 1 : 2));
  g_variant_builder_add(&values, "{sv}", "Connected", g_variant_new_boolean(fixture.connected));
  g_variant_builder_add(&values, "{sv}", "ServicesResolved", g_variant_new_boolean(fixture.connected));
  if (!fixture.absent_rssi)
    g_variant_builder_add(&values, "{sv}", "RSSI", fixture.invalid_rssi ?
      g_variant_new_string("not an int16") : g_variant_new_int16(fixture.rssi));
  return g_variant_builder_end(&values);
}

static GVariant *managed_objects(void)
{
  GVariantBuilder objects; g_variant_builder_init(&objects, G_VARIANT_TYPE("a{oa{sa{sv}}}"));
  const char *paths[] = {RADIO, OTHER_RADIO, PEER};
  for (guint i = 0; i < G_N_ELEMENTS(paths); i++) {
    if ((i == 0 && fixture.no_adapter) || (i == 1 && !fixture.multiple)) continue;
    GVariantBuilder interfaces;
    g_variant_builder_init(&interfaces, G_VARIANT_TYPE("a{sa{sv}}"));
    g_variant_builder_add(&interfaces, "{s@a{sv}}", i == 2 ? DEVICE : ADAPTER,
      i == 2 ? device_values() : adapter_values());
    g_variant_builder_add(&objects, "{oa{sa{sv}}}", paths[i], &interfaces);
  }
  for (guint version = 1; version <= 2 + (guint)fixture.duplicate_service; version++) {
    char parent[256]; g_snprintf(parent, sizeof(parent), PEER "/service_%u", version);
    GVariantBuilder interfaces, values;
    g_variant_builder_init(&interfaces, G_VARIANT_TYPE("a{sa{sv}}"));
    g_variant_builder_init(&values, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&values, "{sv}", "UUID", g_variant_new_string(version == 2 ? SERVICE_V2 : SERVICE));
    g_variant_builder_add(&values, "{sv}", "Device", g_variant_new_object_path(PEER));
    g_variant_builder_add(&interfaces, "{sa{sv}}", B_SERVICE, &values);
    g_variant_builder_add(&objects, "{oa{sa{sv}}}", parent, &interfaces);
    for (guint index = 2; index <= 5 + (guint)fixture.duplicate_char; index++) {
      char path[256], uuid[37]; uint8_t bytes[16]; memcpy(bytes, service_uuid, 16); bytes[15] = index == 6 ? 2 : index;
      bluez_uuid(bytes, uuid); g_snprintf(path, sizeof(path), "%s/char_%u", parent, index);
      g_variant_builder_init(&interfaces, G_VARIANT_TYPE("a{sa{sv}}"));
      g_variant_builder_init(&values, G_VARIANT_TYPE_VARDICT);
      g_variant_builder_add(&values, "{sv}", "UUID", g_variant_new_string(uuid));
      g_variant_builder_add(&values, "{sv}", "Service", g_variant_new_object_path(parent));
      const char *flags[] = {"read", "write"};
      g_variant_builder_add(&values, "{sv}", "Flags", g_variant_new_strv(flags, index == 2 ? 2 : 1));
      g_variant_builder_add(&interfaces, "{sa{sv}}", B_CHAR, &values);
      g_variant_builder_add(&objects, "{oa{sa{sv}}}", path, &interfaces);
    }
  }
  return g_variant_new("(a{oa{sa{sv}}})", &objects);
}

static void emit_connected(GDBusConnection *service)
{
  GVariantBuilder values; g_variant_builder_init(&values, G_VARIANT_TYPE_VARDICT);
  g_variant_builder_add(&values, "{sv}", "Connected", g_variant_new_boolean(fixture.connected));
  g_variant_builder_add(&values, "{sv}", "ServicesResolved", g_variant_new_boolean(fixture.connected));
  assert(g_dbus_connection_emit_signal(service, NULL, PEER, PROPERTIES, "PropertiesChanged",
    g_variant_new("(sa{sv}@as)", DEVICE, &values, g_variant_new_strv(NULL, 0)), NULL));
}

/* Caller holds lock. Both an existing cached device and a new device can
 * receive RF updates; UUID-only cache changes must not become fresh reports. */
static void emit_report(GDBusConnection *service, const char *path, gboolean added)
{
  GVariantBuilder values; g_variant_builder_init(&values, G_VARIANT_TYPE_VARDICT);
  if (fixture.uuid_only) {
    const char *uuid = SERVICE;
    g_variant_builder_add(&values, "{sv}", "UUIDs", g_variant_new_strv(&uuid, 1));
  } else {
    g_variant_builder_add(&values, "{sv}", "RSSI", g_variant_new_int16(-51));
  }
  GVariant *body;
  if (added) {
    GVariantBuilder interfaces;
    g_variant_builder_init(&interfaces, G_VARIANT_TYPE("a{sa{sv}}"));
    g_variant_builder_add(&interfaces, "{sa{sv}}", DEVICE, &values);
    body = g_variant_new("(oa{sa{sv}})", path, &interfaces);
  } else {
    body = g_variant_new("(sa{sv}@as)", DEVICE, &values, g_variant_new_strv(NULL, 0));
  }
  g_autoptr(GError) error = NULL;
  assert(g_dbus_connection_emit_signal(service, NULL, added ? "/" : path,
    added ? OBJECTS : PROPERTIES, added ? "InterfacesAdded" : "PropertiesChanged", body, &error));
}

static GDBusMessage *serve(GDBusConnection *connection, GDBusMessage *message,
                           gboolean incoming, gpointer unused)
{
  (void)unused;
  if (!incoming || g_dbus_message_get_message_type(message) != G_DBUS_MESSAGE_TYPE_METHOD_CALL)
    return message;
  const char *method = g_dbus_message_get_member(message);
  const char *interface = g_dbus_message_get_interface(message);
  const char *path = g_dbus_message_get_path(message);
  assert(strcmp(g_dbus_message_get_destination(message),
    g_dbus_connection_get_unique_name(connection)) == 0);
  GDBusMessage *reply;
  GVariant *body;
  g_mutex_lock(&lock);
  if (fixture.denied) {
    reply = g_dbus_message_new_method_error_literal(message,
      "org.freedesktop.DBus.Error.AccessDenied", "fixture denied");
    goto respond;
  }
  if (strcmp(method, "GetManagedObjects") == 0) {
    assert(strcmp(interface, OBJECTS) == 0 && strcmp(path, "/") == 0);
    body = managed_objects();
  } else if (strcmp(method, "GetAll") == 0) {
    assert(strcmp(interface, PROPERTIES) == 0);
    const char *requested; g_variant_get(g_dbus_message_get_body(message), "(&s)", &requested);
    assert(strcmp(requested, ADAPTER) == 0 || strcmp(requested, DEVICE) == 0);
    body = g_variant_new("(@a{sv})", strcmp(requested, ADAPTER) == 0 ? adapter_values() : device_values());
  } else if (strcmp(interface, B_DEVICE) == 0) {
    assert(strcmp(path, PEER) == 0);
    if (strcmp(method, "Connect") == 0) {
      fixture.connects++;
      if (fixture.reject_connect) {
        fixture.connected = TRUE; emit_connected(connection);
        reply = g_dbus_message_new_method_error_literal(message, BLUEZ ".Error.InProgress", "fixture other caller");
        goto respond;
      }
      assert(!fixture.connected); fixture.connected = TRUE; fixture.echo_length = 0;
      emit_connected(connection);
      if (fixture.hold_connect) {
        assert(fixture.gatt_pending == NULL); fixture.gatt_pending = message;
        g_mutex_unlock(&lock); return NULL;
      }
    } else {
      assert(strcmp(method, "Disconnect") == 0); fixture.disconnects++;
      if (fixture.deny_disconnects != 0) {
        fixture.deny_disconnects--;
        reply = g_dbus_message_new_method_error_literal(message, BLUEZ ".Error.NotAuthorized", "fixture stop denied");
        goto respond;
      }
      fixture.connected = FALSE; emit_connected(connection);
    }
    body = g_variant_new("()");
  } else if (strcmp(interface, B_CHAR) == 0) {
    assert(fixture.connected);
    if (strcmp(method, "WriteValue") == 0) {
      fixture.gatt_writes++;
      g_autoptr(GVariant) bytes = NULL, options = NULL;
      g_variant_get(g_dbus_message_get_body(message), "(@ay@a{sv})", &bytes, &options);
      const char *type; assert(g_variant_n_children(options) == 1);
      assert(g_variant_lookup(options, "type", "&s", &type) && strcmp(type, "request") == 0);
      const void *data = g_variant_get_fixed_array(bytes, &fixture.echo_length, 1);
      assert(fixture.echo_length <= sizeof(fixture.echo));
      if (fixture.echo_length != 0) memcpy(fixture.echo, data, fixture.echo_length);
      body = g_variant_new("()");
    } else {
      assert(strcmp(method, "ReadValue") == 0); fixture.gatt_reads++;
      g_autoptr(GVariant) options = NULL;
      g_variant_get(g_dbus_message_get_body(message), "(@a{sv})", &options);
      assert(g_variant_n_children(options) == 0);
      if (fixture.read_error != NULL) {
        reply = g_dbus_message_new_method_error_literal(message, fixture.read_error, "fixture remote rejection");
        goto respond;
      }
      unsigned index = path[strlen(path) - 1] - '0'; assert(index >= 2 && index <= 5);
      guint8 value[65]; gsize length = index == 2 ? fixture.echo_length : index == 3 ? 44 : index == 4 ? 64 : 65;
      if (index == 2 && length == 0) {
        reply = g_dbus_message_new_method_error_literal(message, BLUEZ ".Error.Failed",
          "Operation failed with ATT error: 0x0e");
        goto respond;
      }
      if (index == 2) memcpy(value, fixture.echo, length);
      else for (gsize i = 0; i < length; i++) value[i] = length + i;
      body = g_variant_new("(@ay)", g_variant_new_fixed_array(G_VARIANT_TYPE_BYTE, value, length, 1));
    }
  } else {
    assert(strcmp(interface, ADAPTER) == 0 && strcmp(path, RADIO) == 0);
    if (strcmp(method, "SetDiscoveryFilter") == 0) {
      g_autoptr(GVariant) values = NULL;
      g_variant_get(g_dbus_message_get_body(message), "(@a{sv})", &values);
      const char *transport; gboolean duplicates;
      assert(g_variant_n_children(values) == 3);
      assert(g_variant_lookup(values, "Transport", "&s", &transport) && strcmp(transport, "le") == 0);
      assert(g_variant_lookup(values, "DuplicateData", "b", &duplicates) && duplicates);
      g_autoptr(GVariant) uuids = g_variant_lookup_value(values, "UUIDs", G_VARIANT_TYPE_STRING_ARRAY);
      assert(uuids != NULL && g_variant_n_children(uuids) <= 1);
      if (g_variant_n_children(uuids) == 1) {
        const char *uuid; g_variant_get_child(uuids, 0, "&s", &uuid);
        assert(strcmp(uuid, SERVICE) == 0 || strcmp(uuid, SERVICE_V2) == 0);
      }
    } else if (strcmp(method, "StartDiscovery") == 0) {
      fixture.starts++;
      if (fixture.reject_start) {
        reply = g_dbus_message_new_method_error_literal(message,
          BLUEZ ".Error.NotAuthorized", "fixture start denied");
        goto respond;
      }
      assert(!fixture.owned && fixture.caller == NULL);
      fixture.owned = TRUE;
      fixture.caller = g_strdup(g_dbus_message_get_sender(message));
      emit_report(connection, PEER, TRUE);
      if (fixture.lost_start_reply) {
        reply = g_dbus_message_new_method_error_literal(message,
          "org.freedesktop.DBus.Error.NoReply", "fixture lost reply after start");
        goto respond;
      }
      if (fixture.hold_start) {
        assert(fixture.pending == NULL);
        fixture.pending = message;
        g_mutex_unlock(&lock);
        return NULL;
      }
    } else {
      /* No power, pairing, discoverability, trust or cache writes are allowed.
       * The unrelated client's discovery session is not ours to release. */
      assert(strcmp(method, "StopDiscovery") == 0);
      fixture.stops++;
      assert(fixture.owned && strcmp(fixture.caller, g_dbus_message_get_sender(message)) == 0);
      if (fixture.fail_stops != 0) {
        fixture.fail_stops--;
        reply = g_dbus_message_new_method_error_literal(message,
          BLUEZ ".Error.NotAuthorized", "fixture stop denied");
        goto respond;
      }
      fixture.owned = FALSE;
      g_clear_pointer(&fixture.caller, g_free);
    }
    body = g_variant_new("()");
  }
  reply = g_dbus_message_new_method_reply(message);
  g_dbus_message_set_body(reply, body);
respond:
  g_mutex_unlock(&lock);
  assert(g_dbus_connection_send_message(connection, reply, G_DBUS_SEND_MESSAGE_FLAGS_NONE, NULL, NULL));
  g_object_unref(reply); g_object_unref(message);
  return NULL;
}

static void settle(void *context)
{
  /* Test scheduling only, not an RF/discovery deadline or a product delay. */
  gint64 until = g_get_monotonic_time() + 100000;
  do {
    nxrs_ble_scan_test_pump(context);
    g_usleep(1000);
  } while (g_get_monotonic_time() < until);
}

static gint start_done;
static gpointer start_scan(gpointer context)
{
  int status = nxrs_host_ble_scan_start(context, service_uuid);
  g_atomic_int_set(&start_done, status + 1);
  return NULL;
}

static GMutex wake_lock;
static GCond wake_condition;
static gint releases;
static guint wake_generation;
static void gatt_wake(void *unused)
{
  (void)unused;
  g_mutex_lock(&wake_lock); wake_generation++; g_cond_broadcast(&wake_condition); g_mutex_unlock(&wake_lock);
}
static void gatt_release(void *unused)
{
  (void)unused; g_atomic_int_inc(&releases); gatt_wake(NULL);
}
static struct nxrs_gatt_event gatt_next(uint64_t session)
{
  struct nxrs_gatt_event event;
  gint64 deadline = g_get_monotonic_time() + 3000000;
  g_mutex_lock(&wake_lock);
  while (!nxrs_ble_gatt_poll(session, &event))
    assert(g_cond_wait_until(&wake_condition, &wake_lock, deadline));
  g_mutex_unlock(&wake_lock);
  return event;
}
static void gatt_close(uint64_t session)
{
  gint before = g_atomic_int_get(&releases);
  nxrs_ble_gatt_close(session);
  gint64 deadline = g_get_monotonic_time() + 3000000;
  g_mutex_lock(&wake_lock);
  while (g_atomic_int_get(&releases) == before)
    assert(g_cond_wait_until(&wake_condition, &wake_lock, deadline));
  g_mutex_unlock(&wake_lock);
  assert(g_atomic_int_get(&releases) == before + 1);
}
static struct nxrs_gatt_command gatt_command(guint kind, uint64_t operation)
{
  struct nxrs_gatt_command command = {.kind = kind, .operation = operation};
  memcpy(command.service, service_uuid, 16);
  memcpy(command.characteristic, service_uuid, 16); command.characteristic[15] = 2;
  return command;
}
static void gatt_submit(uint64_t session, const struct nxrs_gatt_command *command)
{
  gint64 deadline = g_get_monotonic_time() + 3000000;
  g_mutex_lock(&wake_lock);
  int status;
  while ((status = nxrs_ble_gatt_submit(session, command)) == EBUSY)
    assert(g_cond_wait_until(&wake_condition, &wake_lock, deadline));
  g_mutex_unlock(&wake_lock); assert(status == 0);
}
static void gatt_tests(GDBusConnection *service)
{
  const uint64_t session = 700;
  assert(nxrs_ble_gatt_open(0, 64, gatt_wake, gatt_release, NULL) == EINVAL);
  assert(nxrs_ble_gatt_open(session, 65, gatt_wake, gatt_release, NULL) == EINVAL);
  assert(nxrs_ble_gatt_open(session, 64, gatt_wake, gatt_release, NULL) == 0);
  assert(nxrs_ble_gatt_open(session + 1, 64, gatt_wake, gatt_release, NULL) == EBUSY);
  struct nxrs_gatt_command command = gatt_command(NXRS_GATT_CONNECT, 1);
  bluez_identity(g_dbus_connection_get_unique_name(service), PEER, command.service);
  gatt_submit(session, &command);
  struct nxrs_gatt_event event = gatt_next(session);
  assert(event.kind == NXRS_GATT_CONNECT && event.operation == 1 && event.error == 0);
  command = gatt_command(NXRS_GATT_DISCOVER, 2); gatt_submit(session, &command);
  event = gatt_next(session); assert(event.kind == NXRS_GATT_DISCOVER && event.error == 0 && event.handle == 1);
  command = gatt_command(NXRS_GATT_READ, 3); command.handle = 1; gatt_submit(session, &command);
  event = gatt_next(session); assert(event.kind == NXRS_GATT_READ && event.error == 0 && event.att_error == 14);
  for (guint index = 3; index <= 5; index++) {
    command = gatt_command(NXRS_GATT_DISCOVER, 10 + index);
    command.characteristic[15] = index; gatt_submit(session, &command);
    event = gatt_next(session);
    assert(event.kind == NXRS_GATT_DISCOVER && event.operation == 10 + index && event.error == 0);
    guint handle = index - 1; assert(event.handle == handle);
    command = gatt_command(NXRS_GATT_READ, 20 + index); command.handle = handle;
    gatt_submit(session, &command); event = gatt_next(session);
    gsize expected = index == 3 ? 44 : index == 4 ? 64 : 65;
    assert(event.kind == NXRS_GATT_READ && event.operation == 20 + index);
    if (index == 5) assert(event.error == EINVAL);
    else {
      assert(event.error == 0 && event.length == expected);
      for (gsize i = 0; i < event.length; i++) assert(event.data[i] == (guint8)(expected + i));
    }
  }
  const char *errors[] = {BLUEZ ".Error.NotPermitted", BLUEZ ".Error.InvalidOffset",
                          BLUEZ ".Error.InvalidValueLength", BLUEZ ".Error.InProgress"};
  const int normalized[] = {EACCES, EINVAL, EINVAL, EBUSY};
  for (guint i = 0; i < G_N_ELEMENTS(errors); i++) {
    g_mutex_lock(&lock); fixture.read_error = errors[i]; g_mutex_unlock(&lock);
    command = gatt_command(NXRS_GATT_READ, 30 + i); command.handle = 1; gatt_submit(session, &command);
    event = gatt_next(session);
    assert(event.kind == NXRS_GATT_REMOTE_ERROR && event.operation == 30 + i &&
      event.error == normalized[i] && event.att_error == 0);
  }
  g_mutex_lock(&lock); fixture.read_error = NULL; g_mutex_unlock(&lock);
  command = gatt_command(NXRS_GATT_READ, 34); command.handle = 2; gatt_submit(session, &command);
  event = gatt_next(session);
  assert(event.kind == NXRS_GATT_READ && event.operation == 34 && event.error == 0 && event.length == 44);
  command = gatt_command(NXRS_GATT_WRITE, 35); command.handle = 1; command.length = 20;
  for (guint i = 0; i < command.length; i++) command.data[i] = i;
  gatt_submit(session, &command); event = gatt_next(session);
  assert(event.kind == NXRS_GATT_WRITE && event.error == 0);
  command = gatt_command(NXRS_GATT_READ, 36); command.handle = 1; gatt_submit(session, &command);
  event = gatt_next(session); assert(event.kind == NXRS_GATT_READ && event.error == 0 && event.length == 20);
  for (guint i = 0; i < event.length; i++) assert(event.data[i] == i);
  command = gatt_command(NXRS_GATT_DISCONNECT, 37); gatt_submit(session, &command);
  event = gatt_next(session); assert(event.kind == NXRS_GATT_LINK_LOST_UNKNOWN_REASON && event.operation == 1);
  event = gatt_next(session); assert(event.kind == NXRS_GATT_DISCONNECT && event.operation == 37 && event.error == 0);
  gatt_close(session);
  /* Already-connected peers belong to somebody else. Recovery/drop must not
   * disconnect them, even after a successful local lookup. */
  g_mutex_lock(&lock); fixture.connected = TRUE; guint disconnects = fixture.disconnects; g_mutex_unlock(&lock);
  assert(nxrs_ble_gatt_open(session + 1, 64, gatt_wake, gatt_release, NULL) == 0);
  command = gatt_command(NXRS_GATT_CONNECT, 1);
  bluez_identity(g_dbus_connection_get_unique_name(service), PEER, command.service);
  gatt_submit(session + 1, &command); event = gatt_next(session + 1);
  assert(event.kind == NXRS_GATT_CONNECT && event.error == EBUSY);
  nxrs_ble_gatt_recover(session + 1, 1); event = gatt_next(session + 1);
  assert(event.kind == NXRS_GATT_QUIESCENT && event.operation == 1);
  gatt_close(session + 1);
  g_mutex_lock(&lock); assert(fixture.connected && fixture.disconnects == disconnects);
  fixture.connected = FALSE; fixture.reject_connect = TRUE;
  g_mutex_unlock(&lock);
  assert(nxrs_ble_gatt_open(session + 2, 64, gatt_wake, gatt_release, NULL) == 0);
  command = gatt_command(NXRS_GATT_CONNECT, 1);
  bluez_identity(g_dbus_connection_get_unique_name(service), PEER, command.service);
  gatt_submit(session + 2, &command); event = gatt_next(session + 2);
  assert(event.kind == NXRS_GATT_CONNECT && event.error == EBUSY);
  nxrs_ble_gatt_recover(session + 2, 1); event = gatt_next(session + 2);
  assert(event.kind == NXRS_GATT_QUIESCENT && event.operation == 1);
  gatt_close(session + 2);
  g_mutex_lock(&lock);
  assert(fixture.connected && fixture.disconnects == disconnects);
  fixture.connected = FALSE; fixture.reject_connect = FALSE;
  fixture.hold_connect = TRUE; fixture.deny_disconnects = 1;
  g_mutex_unlock(&lock);
  /* Cancellation can disconnect while Connect's reply remains outstanding,
   * but may not release callback storage or publish quiescence yet. */
  assert(nxrs_ble_gatt_open(session + 3, 64, gatt_wake, gatt_release, NULL) == 0);
  gatt_submit(session + 3, &command);
  gint64 deadline = g_get_monotonic_time() + 2000000;
  for (;;) {
    g_mutex_lock(&lock); gboolean held = fixture.gatt_pending != NULL; g_mutex_unlock(&lock);
    if (held) break;
    assert(g_get_monotonic_time() < deadline); g_usleep(1000);
  }
  nxrs_ble_gatt_recover(session + 3, 1);
  deadline = g_get_monotonic_time() + 2000000;
  for (;;) {
    g_mutex_lock(&lock); gboolean disconnected = !fixture.connected; g_mutex_unlock(&lock);
    if (disconnected) break;
    assert(g_get_monotonic_time() < deadline); g_usleep(1000);
  }
  assert(nxrs_ble_gatt_poll(session + 3, &event) == 0);
  g_mutex_lock(&lock);
  fixture.connected = TRUE; emit_connected(service); /* a late successful link */
  GDBusMessage *late = g_dbus_message_new_method_reply(fixture.gatt_pending);
  g_dbus_message_set_body(late, g_variant_new("()")); g_clear_object(&fixture.gatt_pending);
  fixture.hold_connect = FALSE; g_mutex_unlock(&lock);
  assert(g_dbus_connection_send_message(service, late, G_DBUS_SEND_MESSAGE_FLAGS_NONE, NULL, NULL));
  g_object_unref(late);
  event = gatt_next(session + 3);
  if (event.kind == NXRS_GATT_LINK_LOST_UNKNOWN_REASON) event = gatt_next(session + 3);
  assert(event.kind == NXRS_GATT_QUIESCENT && event.operation == 1);
  gatt_close(session + 3);
  g_mutex_lock(&lock); assert(!fixture.connected); g_mutex_unlock(&lock);
  for (guint duplicate = 0; duplicate < 2; duplicate++) {
    uint64_t owner = session + 4 + duplicate;
    g_mutex_lock(&lock);
    fixture.duplicate_service = duplicate == 0; fixture.duplicate_char = duplicate == 1;
    g_mutex_unlock(&lock);
    assert(nxrs_ble_gatt_open(owner, 64, gatt_wake, gatt_release, NULL) == 0);
    command = gatt_command(NXRS_GATT_CONNECT, 1);
    bluez_identity(g_dbus_connection_get_unique_name(service), PEER, command.service);
    gatt_submit(owner, &command); event = gatt_next(owner); assert(event.error == 0);
    command = gatt_command(NXRS_GATT_DISCOVER, 2); gatt_submit(owner, &command);
    event = gatt_next(owner); assert(event.kind == NXRS_GATT_DISCOVER && event.error == EEXIST);
    gatt_close(owner);
  }
  g_mutex_lock(&lock); fixture.duplicate_service = fixture.duplicate_char = FALSE; g_mutex_unlock(&lock);
  /* A try-lock miss with no queued result must re-arm the single owner wait.
   * A full result queue must not prevent Drop from retiring native storage. */
  assert(nxrs_ble_gatt_open(session + 6, 64, gatt_wake, gatt_release, NULL) == 0);
  g_mutex_lock(&wake_lock); guint before = wake_generation;
  nxrs_linux_gatt_test_contend(session + 6);
  deadline = g_get_monotonic_time() + 2000000;
  while (wake_generation == before) assert(g_cond_wait_until(&wake_condition, &wake_lock, deadline));
  g_mutex_unlock(&wake_lock);
  nxrs_linux_gatt_test_saturate();
  gatt_close(session + 6);
}

int main(int argc, char **argv)
{
  assert(argc >= 1 && argc <= 3);
  g_autoptr(GTestDBus) daemon = g_test_dbus_new(G_TEST_DBUS_NONE);
  g_test_dbus_up(daemon);
  g_setenv("DBUS_SYSTEM_BUS_ADDRESS", g_test_dbus_get_bus_address(daemon), TRUE);
  g_autoptr(GError) error = NULL;
  g_autoptr(GDBusConnection) service = g_dbus_connection_new_for_address_sync(
    g_test_dbus_get_bus_address(daemon), G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT |
    G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION, NULL, NULL, &error);
  assert(service != NULL);
  guint filter = g_dbus_connection_add_filter(service, serve, NULL, NULL);
  void *context = (void *)1;
  assert(nxrs_host_ble_scan_open(NULL) == NXRS_BLE_INVALID);
  assert(nxrs_host_ble_scan_open(&context) == NXRS_BLE_UNAVAILABLE && context == NULL);
  g_autoptr(GVariant) named = g_dbus_connection_call_sync(service,
    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
    g_variant_new("(su)", BLUEZ, 4u), G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 1000, NULL, &error);
  guint32 result; assert(named != NULL); g_variant_get(named, "(u)", &result); assert(result == 1);
  fixture.powered = TRUE; fixture.rssi = -51;
  assert(nxrs_host_ble_scan_open(&context) == NXRS_BLE_OK && context != NULL);
  void *state = context;
  struct nxrs_host_ble_report rows[8]; guint32 count;
  assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_UNAVAILABLE);
  assert(nxrs_host_ble_scan_start(NULL, NULL) == NXRS_BLE_INVALID);
  assert(nxrs_host_ble_scan_get(context, rows, 0, &count) == NXRS_BLE_INVALID);
  assert(nxrs_host_ble_scan_start(context, service_uuid) == NXRS_BLE_OK);
  assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_BUSY);
  settle(state);
  assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_OK && count == 1);
  assert(rows[0].matches_filter == 1 && rows[0].rssi_dbm == -51);
  guint8 token[16]; memcpy(token, rows[0].peer, sizeof(token));
  assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_OK && count == 0);
  for (guint condition = 0; condition < 6; condition++) {
    g_mutex_lock(&lock);
    fixture.uuid_only = condition == 0; fixture.wrong_uuid = condition == 1;
    fixture.wrong_adapter = condition == 2; fixture.absent_rssi = condition == 3;
    fixture.rssi = condition == 4 ? 127 : -51; fixture.invalid_rssi = condition == 5;
    emit_report(service, PEER, FALSE); g_mutex_unlock(&lock);
    settle(state);
    int status = nxrs_host_ble_scan_get(context, rows, 8, &count);
    assert(status == (condition == 5 ? NXRS_BLE_INVALID : NXRS_BLE_OK));
    if (condition < 3) assert(count == 0);
    if (condition == 3 || condition == 4) {
      assert(count == 1 && rows[0].rssi_dbm == INT32_MIN);
      assert(memcmp(rows[0].peer, token, sizeof(token)) == 0);
    }
  }
  g_mutex_lock(&lock); fixture.invalid_rssi = FALSE; fixture.fail_stops = 1; g_mutex_unlock(&lock);
  assert(nxrs_host_ble_scan_stop(context) == NXRS_BLE_DENIED);
  assert(nxrs_ble_scan_test_field(state, 0) && nxrs_ble_scan_test_field(state, 1) && nxrs_ble_scan_test_field(state, 2) == 3);
  assert(nxrs_host_ble_scan_stop(context) == NXRS_BLE_OK);
  assert(!nxrs_ble_scan_test_field(state, 0) && !nxrs_ble_scan_test_field(state, 1) && nxrs_ble_scan_test_field(state, 2) == 0);
  /* Cached GetManagedObjects data and UUID-only signals are not discoveries. */
  g_mutex_lock(&lock); fixture.uuid_only = TRUE; g_mutex_unlock(&lock);
  assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_OK);
  settle(state);
  assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_OK && count == 0);
  for (guint i = 0; i < PENDING_REPORTS + 4; i++) {
    char path[PATH_BYTES]; g_snprintf(path, sizeof(path), PEER "_%u", i);
    nxrs_ble_scan_test_queue(state, path);
  }
  assert(nxrs_ble_scan_test_field(state, 3) == PENDING_REPORTS);
  assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_OK && count == 8);
  assert(nxrs_ble_scan_test_field(state, 3) == PENDING_REPORTS - 8);
  assert(nxrs_host_ble_scan_stop(context) == NXRS_BLE_OK);
  g_mutex_lock(&lock); fixture.reject_start = TRUE; guint stops = fixture.stops; g_mutex_unlock(&lock);
  assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_DENIED);
  assert(!nxrs_ble_scan_test_field(state, 0) && nxrs_ble_scan_test_field(state, 2) == 0);
  g_mutex_lock(&lock); assert(fixture.stops == stops);
  fixture.reject_start = FALSE; fixture.hold_start = TRUE; fixture.fail_stops = 1;
  g_mutex_unlock(&lock);
  GThread *worker = g_thread_new("held-start", start_scan, context);
  gint64 deadline = g_get_monotonic_time() + 2000000;
  for (;;) {
    g_mutex_lock(&lock); gboolean pending = fixture.pending != NULL; g_mutex_unlock(&lock);
    if (pending) break;
    assert(g_get_monotonic_time() < deadline); g_usleep(1000);
  }
  assert(g_atomic_int_get(&start_done) == 0);
  g_atomic_int_set(&clock_offset, 6000000);
  g_usleep(10000);
  assert(g_atomic_int_get(&start_done) == 0); /* no abandoned mutating call */
  g_mutex_lock(&lock);
  GDBusMessage *late = g_dbus_message_new_method_reply(fixture.pending);
  g_dbus_message_set_body(late, g_variant_new("()"));
  g_clear_object(&fixture.pending); fixture.hold_start = FALSE;
  g_mutex_unlock(&lock);
  assert(g_dbus_connection_send_message(service, late, G_DBUS_SEND_MESSAGE_FLAGS_NONE, NULL, NULL));
  g_object_unref(late); g_thread_join(worker); g_atomic_int_set(&clock_offset, 0);
  assert(g_atomic_int_get(&start_done) == NXRS_BLE_TIMEOUT + 1);
  assert(!nxrs_ble_scan_test_field(state, 0) && nxrs_ble_scan_test_field(state, 2) == 0);
  g_mutex_lock(&lock); fixture.lost_start_reply = TRUE; stops = fixture.stops; g_mutex_unlock(&lock);
  assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_TIMEOUT);
  assert(!nxrs_ble_scan_test_field(state, 0) && nxrs_ble_scan_test_field(state, 2) == 0);
  g_mutex_lock(&lock); assert(!fixture.owned && fixture.stops == stops + 1);
  fixture.lost_start_reply = FALSE;
  g_mutex_unlock(&lock);
  nxrs_host_ble_scan_release(context);
  for (guint condition = 0; condition < 5; condition++) {
    g_mutex_lock(&lock);
    fixture.powered = condition != 0; fixture.multiple = condition == 1;
    fixture.denied = condition == 2; fixture.no_adapter = condition == 3;
    fixture.malformed = condition == 4;
    g_mutex_unlock(&lock);
    const int expected[] = {NXRS_BLE_UNAVAILABLE, NXRS_BLE_BUSY, NXRS_BLE_DENIED,
                            NXRS_BLE_UNAVAILABLE, NXRS_BLE_INVALID};
    context = (void *)1;
    assert(nxrs_host_ble_scan_open(&context) == expected[condition] && context == NULL);
  }
  g_mutex_lock(&lock); fixture.malformed = FALSE; g_mutex_unlock(&lock);
  gatt_tests(service);
  if (argc >= 2) {
    g_mutex_lock(&lock);
    assert(fixture.caller == NULL && !fixture.owned);
    memset(&fixture, 0, sizeof(fixture)); fixture.powered = TRUE; fixture.rssi = -51;
    g_mutex_unlock(&lock);
    for (guint injected = 0; injected < 2; injected++) {
      char *arguments[] = {argv[1], injected ? "fail" : NULL, NULL};
      g_autofree char *output = NULL; g_autofree char *errors = NULL; gint status;
      assert(g_spawn_sync(NULL, arguments, NULL, G_SPAWN_DEFAULT, NULL, NULL,
        &output, &errors, &status, &error));
      assert(WIFEXITED(status) && WEXITSTATUS(status) == (injected ? 1 : 0));
      assert(strcmp(output, injected ? "" : "NXRS_BLE_SCAN_PASS peers=1 limited=false shutdown=joined\n") == 0);
      assert(strcmp(errors, injected ? "NXRS_BLE_SCAN_FAIL error=Io\n" : "") == 0);
      g_mutex_lock(&lock); assert(!fixture.owned && fixture.caller == NULL); g_mutex_unlock(&lock);
    }
  }
  if (argc == 3) {
    const char *cases[] = {"legacy", "cancel", "values", "full"};
    for (guint i = 0; i < G_N_ELEMENTS(cases); i++) {
      char *arguments[] = {argv[2], i == 3 ? NULL : (char *)cases[i], NULL};
      g_autofree char *output = NULL, *errors = NULL; gint status;
      assert(g_spawn_sync(NULL, arguments, NULL, G_SPAWN_DEFAULT, NULL, NULL, &output, &errors, &status, &error));
      g_print("CASE %s\n%sEND\n", cases[i], output);
      assert(WIFEXITED(status) && WEXITSTATUS(status) == 0 && strcmp(errors, "") == 0);
      g_mutex_lock(&lock);
      assert(!fixture.connected && !fixture.owned && fixture.gatt_pending == NULL);
      g_mutex_unlock(&lock);
    }
  }
  /* A daemon disappearance must fault even when no peer reports are pending.
   * Teardown can then retire callbacks rather than retrying a dead owner. */
  g_mutex_lock(&lock); fixture.malformed = FALSE; g_mutex_unlock(&lock);
  assert(nxrs_host_ble_scan_open(&context) == NXRS_BLE_OK);
  assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_OK);
  assert(nxrs_ble_gatt_open(900, 64, gatt_wake, gatt_release, NULL) == 0);
  struct nxrs_gatt_command connect = gatt_command(NXRS_GATT_CONNECT, 1);
  bluez_identity(g_dbus_connection_get_unique_name(service), PEER, connect.service);
  gatt_submit(900, &connect);
  struct nxrs_gatt_event gatt = gatt_next(900);
  assert(gatt.kind == NXRS_GATT_CONNECT && gatt.error == 0);
  g_dbus_connection_remove_filter(service, filter);
  assert(g_dbus_connection_close_sync(service, NULL, &error));
  settle(context);
  assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_UNAVAILABLE);
  assert(nxrs_host_ble_scan_stop(context) == NXRS_BLE_OK);
  assert(nxrs_ble_scan_test_field(context, 2) == 0);
  nxrs_host_ble_scan_release(context);
  gatt = gatt_next(900);
  assert(gatt.kind == NXRS_GATT_LINK_LOST_UNKNOWN_REASON && gatt.operation == 1);
  nxrs_ble_gatt_recover(900, 1); gatt = gatt_next(900);
  assert(gatt.kind == NXRS_GATT_QUIESCENT && gatt.operation == 1);
  gatt_close(900);
  g_mutex_lock(&lock); fixture.owned = FALSE; g_clear_pointer(&fixture.caller, g_free); g_mutex_unlock(&lock);
  assert(nxrs_host_ble_scan_open(&context) == NXRS_BLE_UNAVAILABLE && context == NULL);
  g_test_dbus_down(daemon);
  return 0;
}
