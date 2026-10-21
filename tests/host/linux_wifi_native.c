#include "native_radio_abi/wifi.h"
/* Actual production bridge over a private D-Bus daemon and fake NetworkManager.
 * This tests IPC, schemas and ownership, not Linux wireless hardware.
 */
#include <gio/gio.h>
#include <assert.h>
#include <string.h>
#include <sys/wait.h>
#include <stdbool.h>
#include <stdint.h>

static gboolean fast_time;
static gint64 elapsed;
int64_t nxrs_radio_test_clock(int64_t real) {return fast_time ? real+(elapsed+=100000) : real;}
bool nxrs_radio_test_fast(void) {return fast_time;}
#include "../../hal/wifi/src/providers/native/linux/ffi/gio.c"
#define NM "org.freedesktop.NetworkManager"
#define NM_PATH "/org/freedesktop/NetworkManager"
#define DEVICE NM ".Device"
#define WIRELESS DEVICE ".Wireless"
#define AP NM ".AccessPoint"
#define ACTIVE NM ".Connection.Active"

static GMutex lock;
static struct {
  gboolean powered, denied, multiple, stale, malformed, associated, addressed;
  guint ssid_length;
  guint ap_count;
  guint requests;
  gint64 last_scan;
  const char *ip4, *ip6;
  gboolean owned, hold_activation, hold_deactivation, reject_activation;
  gboolean no_address, replaced, lost_deactivation_reply;
  guint activations, deactivations;
  guint fail_deactivation_at;
  GDBusMessage *pending;
  char *last_uuid;
} fixture;

#define WIFI_PATH NM_PATH "/Devices/1"
#define OTHER_PATH NM_PATH "/Devices/2"
#define AP_PATH NM_PATH "/AccessPoint/"
#define ACTIVE_PATH NM_PATH "/ActiveConnection/1"
#define EXTERNAL_PATH NM_PATH "/ActiveConnection/2"

static GVariant *activation_reply(void)
{
  GVariantBuilder result; g_variant_builder_init(&result, G_VARIANT_TYPE_VARDICT);
  return g_variant_new("(ooa{sv})", NM_PATH "/Settings/1", ACTIVE_PATH, &result);
}

static void validate_activation(GDBusMessage *message)
{
  g_autoptr(GVariant) settings = NULL;
  g_autoptr(GVariant) options = NULL;
  const char *device, *specific, *value;
  g_variant_get(g_dbus_message_get_body(message), "(@a{sa{sv}}&o&o@a{sv})",
    &settings, &device, &specific, &options);
  assert(strcmp(device, WIFI_PATH) == 0 && strcmp(specific, "/") == 0);
  assert(g_variant_lookup(options, "persist", "&s", &value) && strcmp(value, "volatile") == 0);
  assert(g_variant_lookup(options, "bind-activation", "&s", &value) && strcmp(value, "dbus-client") == 0);
  assert(g_variant_n_children(options) == 2);
  g_autoptr(GVariant) connection = g_variant_lookup_value(settings, "connection", G_VARIANT_TYPE_VARDICT);
  gboolean autoconnect = TRUE;
  assert(connection != NULL);
  assert(g_variant_lookup(connection, "autoconnect", "b", &autoconnect) && !autoconnect);
  assert(g_variant_lookup(connection, "uuid", "&s", &value) && g_uuid_string_is_valid(value));
  assert(g_strcmp0(value, fixture.last_uuid) != 0);
  g_free(fixture.last_uuid); fixture.last_uuid = g_strdup(value);
  assert(g_variant_lookup(connection, "type", "&s", &value) && strcmp(value, "802-11-wireless") == 0);
  g_autoptr(GVariant) wireless = g_variant_lookup_value(settings, "802-11-wireless", G_VARIANT_TYPE_VARDICT);
  assert(wireless != NULL);
  g_autoptr(GVariant) ssid = g_variant_lookup_value(wireless, "ssid", G_VARIANT_TYPE("ay"));
  gsize length; const guint8 *bytes = g_variant_get_fixed_array(ssid, &length, 1);
  const guint8 expected[] = {0xff, 0, 0x7f};
  assert(length == sizeof(expected) && memcmp(bytes, expected, sizeof(expected)) == 0);
  g_autoptr(GVariant) security = g_variant_lookup_value(settings, "802-11-wireless-security", G_VARIANT_TYPE_VARDICT);
  if (security != NULL) {
    guint32 flags;
    assert(g_variant_lookup(security, "key-mgmt", "&s", &value) && strcmp(value, "wpa-psk") == 0);
    assert(g_variant_lookup(security, "psk", "&s", &value) && strcmp(value, "fixture-only-password") == 0);
    assert(g_variant_lookup(security, "psk-flags", "u", &flags) && flags == 2);
    g_autoptr(GVariant) protocols = g_variant_lookup_value(security, "proto", G_VARIANT_TYPE("as"));
    assert(protocols != NULL && g_variant_n_children(protocols) == 1);
    g_variant_get_child(protocols, 0, "&s", &value); assert(strcmp(value, "rsn") == 0);
  }
}

static GVariant *property_reply(const char *path, const char *interface)
{
  GVariantBuilder values; g_variant_builder_init(&values, G_VARIANT_TYPE_VARDICT);
  if (strcmp(interface, NM) == 0) {
    g_variant_builder_add(&values, "{sv}", "WirelessEnabled", g_variant_new_boolean(fixture.powered));
    g_variant_builder_add(&values, "{sv}", "WirelessHardwareEnabled", g_variant_new_boolean(TRUE));
    const char *paths[] = {ACTIVE_PATH, NULL};
    g_variant_builder_add(&values, "{sv}", "ActiveConnections", g_variant_new_objv(paths, fixture.owned ? 1 : 0));
  } else if (strcmp(interface, ACTIVE) == 0) {
    assert(strcmp(path, ACTIVE_PATH) == 0);
    g_variant_builder_add(&values, "{sv}", "State", g_variant_new_uint32(fixture.owned ? 2 : 4));
  } else if (strcmp(interface, DEVICE) == 0) {
    g_variant_builder_add(&values, "{sv}", "DeviceType", g_variant_new_uint32(2));
    g_variant_builder_add(&values, "{sv}", "Managed", g_variant_new_boolean(TRUE));
    g_variant_builder_add(&values, "{sv}", "ActiveConnection", g_variant_new_object_path(
      fixture.replaced ? EXTERNAL_PATH : fixture.owned ? ACTIVE_PATH : fixture.associated ? EXTERNAL_PATH : "/"));
    g_variant_builder_add(&values, "{sv}", "State", g_variant_new_uint32(fixture.addressed ? 100 : 70));
    g_variant_builder_add(&values, "{sv}", "Ip4Config", g_variant_new_object_path(fixture.addressed ? NM_PATH "/IP4Config/1" : "/"));
    g_variant_builder_add(&values, "{sv}", "Ip6Config", g_variant_new_object_path(fixture.addressed ? NM_PATH "/IP6Config/1" : "/"));
  } else if (strcmp(interface, NM ".IP4Config") == 0 || strcmp(interface, NM ".IP6Config") == 0) {
    gboolean ipv6 = strcmp(interface, NM ".IP6Config") == 0;
    const char *text = ipv6 ? fixture.ip6 : fixture.ip4;
    GVariantBuilder addresses; g_variant_builder_init(&addresses, G_VARIANT_TYPE("aa{sv}"));
    if (text != NULL) {
      GVariantBuilder address; g_variant_builder_init(&address, G_VARIANT_TYPE_VARDICT);
      g_variant_builder_add(&address, "{sv}", "address", g_variant_new_string(text));
      g_variant_builder_add(&address, "{sv}", "prefix", g_variant_new_uint32(ipv6 ? 64 : 24));
      g_variant_builder_add(&addresses, "a{sv}", &address);
    }
    g_variant_builder_add(&values, "{sv}", "AddressData", g_variant_builder_end(&addresses));
  } else if (strcmp(interface, WIRELESS) == 0) {
    if (!fixture.malformed)
      g_variant_builder_add(&values, "{sv}", "LastScan", g_variant_new_int64(fixture.last_scan));
    g_variant_builder_add(&values, "{sv}", "ActiveAccessPoint", g_variant_new_object_path(fixture.associated ? AP_PATH "0" : "/"));
  } else {
    assert(strcmp(interface, AP) == 0);
    guint index = path[strlen(path) - 1] - '0';
    assert(index < 4);
    const guint8 bytes[33] = {0xff, 0, 0x7f};
    g_variant_builder_add(&values, "{sv}", "Ssid", g_variant_new_fixed_array(
      G_VARIANT_TYPE_BYTE, bytes, index == 3 ? 0 : fixture.ssid_length, 1));
    g_variant_builder_add(&values, "{sv}", "Flags", g_variant_new_uint32(index == 1 || index == 2 ? 1 : 0));
    g_variant_builder_add(&values, "{sv}", "WpaFlags", g_variant_new_uint32(0));
    g_variant_builder_add(&values, "{sv}", "RsnFlags", g_variant_new_uint32(index == 1 ? 0x100 : index == 2 ? 0x200 : 0));
    /* Quality is intentionally unrelated to dBm and must not be converted. */
    g_variant_builder_add(&values, "{sv}", "Strength", g_variant_new_byte(89));
  }
  return g_variant_new("(a{sv})", &values);
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
  /* Every operation stays with the original daemon, not a same-path object
   * exported by a replacement process under the well-known name. */
  assert(strcmp(g_dbus_message_get_destination(message),
    g_dbus_connection_get_unique_name(connection)) == 0);
  GDBusMessage *reply;
  g_mutex_lock(&lock);
  if (fixture.denied) {
    reply = g_dbus_message_new_method_error_literal(message, "org.freedesktop.DBus.Error.AccessDenied", "fixture denied");
  } else {
    GVariant *body;
    if (strcmp(method, "GetAll") == 0) {
      assert(strcmp(interface, "org.freedesktop.DBus.Properties") == 0);
      const char *requested; g_variant_get(g_dbus_message_get_body(message), "(&s)", &requested);
      body = property_reply(path, requested);
    } else if (strcmp(method, "GetDevices") == 0) {
      assert(strcmp(interface, NM) == 0 && strcmp(path, NM_PATH) == 0);
      const char *devices[] = {WIFI_PATH, OTHER_PATH, NULL};
      body = g_variant_new("(@ao)", g_variant_new_objv(devices, fixture.multiple ? 2 : 1));
    } else if (strcmp(method, "RequestScan") == 0) {
      assert(strcmp(interface, WIRELESS) == 0 && strcmp(path, WIFI_PATH) == 0);
      assert(g_variant_is_of_type(g_dbus_message_get_body(message), G_VARIANT_TYPE("(a{sv})")));
      fixture.requests++;
      if (!fixture.stale) fixture.last_scan++;
      body = g_variant_new("()");
    } else if (strcmp(method, "AddAndActivateConnection2") == 0) {
      assert(strcmp(interface, NM) == 0 && strcmp(path, NM_PATH) == 0);
      validate_activation(message); fixture.activations++;
      if (fixture.reject_activation) {
        reply = g_dbus_message_new_method_error_literal(message,
          "org.freedesktop.DBus.Error.UnknownMethod", "fixture unsupported");
        goto respond;
      }
      fixture.owned = TRUE; fixture.associated = TRUE;
      fixture.addressed = !fixture.no_address; fixture.ip4 = "10.42.0.2"; fixture.ip6 = NULL;
      if (fixture.hold_activation) {
        assert(fixture.pending == NULL);
        fixture.pending = message;
        g_mutex_unlock(&lock);
        return NULL;
      }
      body = activation_reply();
    } else if (strcmp(method, "DeactivateConnection") == 0) {
      const char *active; g_variant_get(g_dbus_message_get_body(message), "(&o)", &active);
      assert(strcmp(interface, NM) == 0 && strcmp(path, NM_PATH) == 0 && strcmp(active, ACTIVE_PATH) == 0);
      fixture.deactivations++;
      if (!fixture.hold_deactivation && fixture.deactivations != fixture.fail_deactivation_at) {
        fixture.owned = FALSE;
        if (!fixture.replaced) fixture.associated = fixture.addressed = FALSE;
      }
      if (fixture.lost_deactivation_reply) {
        reply = g_dbus_message_new_method_error_literal(message,
          "org.freedesktop.DBus.Error.NoReply", "fixture lost reply");
        goto respond;
      }
      body = g_variant_new("()");
    } else {
      assert(strcmp(method, "GetAllAccessPoints") == 0 && strcmp(interface, WIRELESS) == 0);
      const char *paths[] = {AP_PATH "0", AP_PATH "1", AP_PATH "2", AP_PATH "3"};
      const char *aps[65];
      for (guint i = 0; i < G_N_ELEMENTS(aps); i++) aps[i] = paths[i % G_N_ELEMENTS(paths)];
      body = g_variant_new("(@ao)", g_variant_new_objv(aps, fixture.ap_count ? fixture.ap_count : 4));
    }
    reply = g_dbus_message_new_method_reply(message);
    g_dbus_message_set_body(reply, body);
  }
respond:
  g_mutex_unlock(&lock);
  g_autoptr(GError) error = NULL;
  assert(g_dbus_connection_send_message(connection, reply, G_DBUS_SEND_MESSAGE_FLAGS_NONE, NULL, &error));
  g_object_unref(reply);
  g_object_unref(message);
  return NULL;
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
  g_autoptr(GVariant) named = g_dbus_connection_call_sync(service,
    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
    g_variant_new("(su)", NM, 4u), G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 1000, NULL, &error);
  guint32 result; assert(named != NULL); g_variant_get(named, "(u)", &result); assert(result == 1);
  fixture.powered = TRUE; fixture.ssid_length = 3;
  void *context = NULL;
  assert(nxrs_host_wifi_open(NULL, NXRS_WIFI_VOLATILE) == NXRS_WIFI_INVALID);
  assert(nxrs_host_wifi_open(&context, 2) == NXRS_WIFI_INVALID && context == NULL);
  assert(nxrs_host_wifi_open(&context, NXRS_WIFI_VOLATILE) == NXRS_WIFI_OK && context != NULL);
  struct nxrs_host_wifi_ap rows[4]; guint32 count, truncated, link;
  assert(nxrs_host_wifi_scan(context, rows, 4, &count, &truncated) == NXRS_WIFI_OK);
  assert(count == 4 && truncated == 0);
  const guint8 expected[] = {0xff, 0, 0x7f};
  assert(rows[0].ssid_length == sizeof(expected) && memcmp(rows[0].ssid, expected, sizeof(expected)) == 0);
  for (guint i = 0; i < 4; i++) assert(rows[i].rssi_dbm == INT32_MIN);
  assert(rows[0].security == 0 && rows[1].security == 1 && rows[2].security == 2);
  assert(rows[3].ssid_length == 0);
  assert(nxrs_host_wifi_scan(context, rows, 2, &count, &truncated) == NXRS_WIFI_OK && count == 2 && truncated == 1);
  struct nxrs_host_wifi_ap bounded[64];
  g_mutex_lock(&lock); fixture.ap_count = 64; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_scan(context, bounded, 64, &count, &truncated) == NXRS_WIFI_OK && count == 64 && truncated == 0);
  g_mutex_lock(&lock); fixture.ap_count = 65; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_scan(context, bounded, 64, &count, &truncated) == NXRS_WIFI_OK && count == 64 && truncated == 1);
  g_mutex_lock(&lock); fixture.ap_count = 0; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_scan(context, rows, 0, &count, &truncated) == NXRS_WIFI_INVALID);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 1);
  g_mutex_lock(&lock); fixture.associated = TRUE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 2);
  g_mutex_lock(&lock); fixture.addressed = TRUE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 2); /* empty configs */
  g_mutex_lock(&lock); fixture.ip4 = "10.42.0.2"; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 3);
  g_mutex_lock(&lock); fixture.ip4 = "169.254.1.2"; fixture.ip6 = "fe80::1"; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 2);
  g_mutex_lock(&lock); fixture.ip6 = "fd00::2"; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 3);
  g_mutex_lock(&lock); fixture.ip6 = "not-an-address"; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_PROTOCOL);
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_BUSY);
  assert(nxrs_host_wifi_disconnect(context) == NXRS_WIFI_BUSY);
  g_mutex_lock(&lock); assert(fixture.activations == 0 && fixture.deactivations == 0);
  fixture.associated = fixture.addressed = FALSE;
  g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_connect(context, NULL, 0, NULL, 0) == NXRS_WIFI_INVALID);
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 8) == NXRS_WIFI_INVALID);
  assert(nxrs_host_wifi_connect(context, expected, 3, (const guint8 *)"short", 5) == NXRS_WIFI_INVALID);
  const guint8 invalid_password[] = "invalid\npassword";
  assert(nxrs_host_wifi_connect(context, expected, 3, invalid_password, sizeof(invalid_password) - 1) == NXRS_WIFI_INVALID);
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_OK);
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_BUSY);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 3);
  assert(nxrs_host_wifi_disconnect(context) == NXRS_WIFI_OK);
  assert(nxrs_host_wifi_disconnect(context) == NXRS_WIFI_BUSY);
  const guint8 password[] = "fixture-only-password";
  assert(nxrs_host_wifi_connect(context, expected, 3, password, sizeof(password) - 1) == NXRS_WIFI_OK);
  g_mutex_lock(&lock); fixture.replaced = TRUE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 1);
  assert(nxrs_host_wifi_release(context) == NXRS_WIFI_OK);
  g_mutex_lock(&lock);
  assert(fixture.associated && fixture.activations == 2 && fixture.deactivations == 2);
  fixture.replaced = fixture.associated = fixture.addressed = FALSE;
  fixture.reject_activation = TRUE;
  g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_open(&context, NXRS_WIFI_SYSTEM_MANAGED) == NXRS_WIFI_OK);
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_UNSUPPORTED);
  g_mutex_lock(&lock); assert(fixture.activations == 3 && fixture.deactivations == 2);
  fixture.reject_activation = FALSE; fixture.no_address = TRUE;
  g_mutex_unlock(&lock);
  fast_time = TRUE; elapsed = 0;
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_TIMEOUT);
  fast_time = FALSE;
  g_mutex_lock(&lock); assert(!fixture.owned); fixture.no_address = FALSE; g_mutex_unlock(&lock);
  /* The daemon has accepted activation but withholds the reply. A local
   * deadline must not discard the callback, free state or permit retry. */
  g_mutex_lock(&lock); fixture.hold_activation = TRUE; g_mutex_unlock(&lock);
  fast_time = TRUE; elapsed = 0;
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_IO);
  fast_time = FALSE;
  assert(nxrs_host_wifi_release(context) == NXRS_WIFI_BUSY);
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_BUSY);
  g_mutex_lock(&lock);
  assert(fixture.pending != NULL && fixture.owned);
  GDBusMessage *late = g_dbus_message_new_method_reply(fixture.pending);
  g_dbus_message_set_body(late, activation_reply());
  g_clear_object(&fixture.pending); fixture.hold_activation = FALSE;
  g_mutex_unlock(&lock);
  assert(g_dbus_connection_send_message(service, late, G_DBUS_SEND_MESSAGE_FLAGS_NONE, NULL, &error));
  g_object_unref(late);
  gint64 until = g_get_monotonic_time() + 1000000;
  int closed;
  while ((closed = nxrs_host_wifi_release(context)) == NXRS_WIFI_BUSY &&
      g_get_monotonic_time() < until) g_usleep(1000);
  assert(closed == NXRS_WIFI_OK);
  g_mutex_lock(&lock); assert(!fixture.owned); g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_open(&context, NXRS_WIFI_SYSTEM_MANAGED) == NXRS_WIFI_OK);
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_OK);
  g_mutex_lock(&lock); fixture.hold_deactivation = TRUE; g_mutex_unlock(&lock);
  fast_time = TRUE; elapsed = 0;
  assert(nxrs_host_wifi_release(context) == NXRS_WIFI_IO);
  fast_time = FALSE;
  g_mutex_lock(&lock); assert(fixture.owned); fixture.hold_deactivation = FALSE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_release(context) == NXRS_WIFI_OK);
  g_mutex_lock(&lock); assert(!fixture.owned); g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_open(&context, NXRS_WIFI_VOLATILE) == NXRS_WIFI_OK);
  assert(nxrs_host_wifi_connect(context, expected, 3, NULL, 0) == NXRS_WIFI_OK);
  g_mutex_lock(&lock); fixture.lost_deactivation_reply = TRUE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_disconnect(context) == NXRS_WIFI_OK); /* independent inactive proof */
  g_mutex_lock(&lock); fixture.lost_deactivation_reply = FALSE; g_mutex_unlock(&lock);
  g_mutex_lock(&lock); fixture.ssid_length = 33; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_scan(context, rows, 4, &count, &truncated) == NXRS_WIFI_PROTOCOL);
  g_mutex_lock(&lock); fixture.ssid_length = 3; fixture.malformed = TRUE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_scan(context, rows, 4, &count, &truncated) == NXRS_WIFI_PROTOCOL);
  g_mutex_lock(&lock); fixture.malformed = FALSE; fixture.stale = TRUE; g_mutex_unlock(&lock);
  fast_time = TRUE; elapsed = 0;
  assert(nxrs_host_wifi_scan(context, rows, 4, &count, &truncated) == NXRS_WIFI_TIMEOUT);
  fast_time = FALSE;
  assert(nxrs_host_wifi_release(context) == NXRS_WIFI_OK); context = (void *)1;
  g_mutex_lock(&lock); fixture.powered = FALSE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_open(&context, NXRS_WIFI_VOLATILE) == NXRS_WIFI_UNAVAILABLE && context == NULL);
  g_mutex_lock(&lock); fixture.powered = TRUE; fixture.multiple = TRUE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_open(&context, NXRS_WIFI_VOLATILE) == NXRS_WIFI_BUSY && context == NULL);
  g_mutex_lock(&lock); fixture.multiple = FALSE; fixture.denied = TRUE; g_mutex_unlock(&lock);
  assert(nxrs_host_wifi_open(&context, NXRS_WIFI_VOLATILE) == NXRS_WIFI_DENIED && context == NULL);
  g_mutex_lock(&lock); fixture.denied = FALSE; g_mutex_unlock(&lock);
  if (argc >= 2) {
    /* The exact portable Rust owner must complete and join against the same
     * private service. The injected failure must not turn into a scan PASS. */
    g_mutex_lock(&lock);
    g_clear_pointer(&fixture.last_uuid, g_free);
    memset(&fixture, 0, sizeof(fixture)); fixture.powered = TRUE; fixture.ssid_length = 3;
    g_mutex_unlock(&lock);
    for (guint injected = 0; injected < 2; injected++) {
      char *arguments[] = {argv[1], injected ? "fail" : NULL, NULL};
      g_autofree char *output = NULL;
      g_autofree char *errors = NULL;
      gint status;
      assert(g_spawn_sync(NULL, arguments, NULL, G_SPAWN_DEFAULT, NULL, NULL,
        &output, &errors, &status, &error));
      assert(WIFEXITED(status) && WEXITSTATUS(status) == (injected ? 7 : 0));
      assert(strcmp(errors, "") == 0);
      assert(strcmp(output, injected ? "NXRS_WIFI_SCAN_INJECTED_FAILURE\n" :
        "NXRS_WIFI_SCAN_PASS results=4 shutdown=joined\n") == 0);
    }
  }
  if (argc == 3) {
    guint activations, deactivations;
    g_mutex_lock(&lock);
    activations = fixture.activations; deactivations = fixture.deactivations;
    fixture.fail_deactivation_at = deactivations + 2;
    g_mutex_unlock(&lock);
    char *arguments[] = {argv[2], "--ignored", "--exact", "station_owner", "--nocapture", NULL};
    g_auto(GStrv) environment = g_get_environ();
    environment = g_environ_setenv(environment, "NXRS_PRIVATE_RADIO_TEST", "1", TRUE);
    g_autofree char *output = NULL;
    g_autofree char *errors = NULL;
    gint status;
    assert(g_spawn_sync(NULL, arguments, environment, G_SPAWN_DEFAULT, NULL, NULL,
      &output, &errors, &status, &error));
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    assert(strcmp(errors, "") == 0);
    assert(strstr(output, "test result: ok. 1 passed; 0 failed; 0 ignored;") != NULL);
    g_mutex_lock(&lock);
    assert(fixture.activations == activations + 2 && fixture.deactivations == deactivations + 3);
    assert(!fixture.owned && !fixture.associated && fixture.pending == NULL);
    g_mutex_unlock(&lock);
  }
  g_dbus_connection_remove_filter(service, filter);
  assert(g_dbus_connection_close_sync(service, NULL, &error));
  assert(nxrs_host_wifi_open(&context, NXRS_WIFI_VOLATILE) == NXRS_WIFI_UNAVAILABLE && context == NULL);
  g_clear_pointer(&fixture.last_uuid, g_free);
  g_test_dbus_down(daemon);
  return 0;
}
