/* NetworkManager on the existing Wi-Fi worker. Only Connect creates a volatile,
 * client-bound association; observation never changes radio/profile settings.
 */
#include <gio/gio.h>
#include <stdbool.h>
#include <string.h>
#include "../native/wifi.h"

#define NM "org.freedesktop.NetworkManager"
#define NM_PATH "/org/freedesktop/NetworkManager"
#define DEVICE NM ".Device"
#define WIRELESS DEVICE ".Wireless"
#define AP NM ".AccessPoint"
#define ACTIVE NM ".Connection.Active"

struct wifi_context {
  GDBusConnection *bus;
  GMainContext *events;
  char *daemon;
  char *device;
  char *active;
  GVariant *activation_reply;
  GError *activation_error;
  bool activating, uncertain;
  gint64 deadline;
};

static int bus_error(GError *error)
{
  if (error == NULL) return NXRS_WIFI_IO;
  if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NO_REPLY) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_TIMEOUT)) return NXRS_WIFI_TIMEOUT;
  if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_AUTH_FAILED)) return NXRS_WIFI_DENIED;
  if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER) ||
      g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CLOSED)) return NXRS_WIFI_UNAVAILABLE;
  if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD)) return NXRS_WIFI_UNSUPPORTED;
  g_autofree char *remote = g_dbus_error_get_remote_error(error);
  if (g_strcmp0(remote, NM ".PermissionDenied") == 0 ||
      g_strcmp0(remote, NM ".NotAuthorized") == 0) return NXRS_WIFI_DENIED;
  if (g_strcmp0(remote, WIRELESS ".ScanNotAllowed") == 0) return NXRS_WIFI_BUSY;
  return NXRS_WIFI_IO;
}

static GVariant *call(struct wifi_context *state, const char *path,
                      const char *interface, const char *method,
                      GVariant *parameters, const char *reply_type, int *status)
{
  gint64 left = state->deadline - g_get_monotonic_time();
  if (left <= 0) {
    if (parameters != NULL) g_variant_unref(g_variant_ref_sink(parameters));
    *status = NXRS_WIFI_TIMEOUT;
    return NULL;
  }
  g_autoptr(GError) error = NULL;
  GVariant *reply = g_dbus_connection_call_sync(state->bus, state->daemon, path, interface,
    method, parameters, G_VARIANT_TYPE(reply_type), G_DBUS_CALL_FLAGS_NO_AUTO_START,
    (gint)MIN((left + 999) / 1000, 2000), NULL, &error);
  *status = reply == NULL ? bus_error(error) : NXRS_WIFI_OK;
  return reply;
}

static GVariant *properties(struct wifi_context *state, const char *path,
                            const char *interface, int *status)
{
  g_autoptr(GVariant) reply = call(state, path, "org.freedesktop.DBus.Properties",
    "GetAll", g_variant_new("(s)", interface), "(a{sv})", status);
  return reply == NULL ? NULL : g_variant_get_child_value(reply, 0);
}

static void release(struct wifi_context *state)
{
  if (state->bus != NULL) {
    g_dbus_connection_close_sync(state->bus, NULL, NULL);
    g_object_unref(state->bus);
  }
  g_clear_pointer(&state->events, g_main_context_unref);
  g_clear_pointer(&state->activation_reply, g_variant_unref);
  g_clear_error(&state->activation_error);
  g_free(state->active);
  g_free(state->daemon);
  g_free(state->device);
  g_free(state);
}

int nxrs_host_wifi_open(void **context, uint32_t storage)
{
  if (context == NULL) return NXRS_WIFI_INVALID;
  *context = NULL;
  if (storage != NXRS_WIFI_VOLATILE && storage != NXRS_WIFI_SYSTEM_MANAGED)
    return NXRS_WIFI_INVALID;
  /* Both policies use volatile, client-bound activation; permission to save
   * never requires it, and older daemons get no persistent fallback. */
  struct wifi_context *state = g_new0(struct wifi_context, 1);
  state->events = g_main_context_new();
  state->deadline = g_get_monotonic_time() + 5000000;
  g_autoptr(GError) error = NULL;
  g_autofree char *address = g_dbus_address_get_for_bus_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
  if (address == NULL) { int status = bus_error(error); release(state); return status; }
  state->bus = g_dbus_connection_new_for_address_sync(address,
    G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
    NULL, NULL, &error);
  if (state->bus == NULL) { int status = bus_error(error); release(state); return status; }
  g_dbus_connection_set_exit_on_close(state->bus, FALSE);
  /* Pin the daemon identity: paths from an old daemon must never identify a
   * different connection after a NetworkManager restart. */
  g_autoptr(GVariant) owner = g_dbus_connection_call_sync(state->bus,
    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
    "GetNameOwner", g_variant_new("(s)", NM), G_VARIANT_TYPE("(s)"),
    G_DBUS_CALL_FLAGS_NO_AUTO_START, 2000, NULL, &error);
  if (owner == NULL) { int status = bus_error(error); release(state); return status; }
  g_variant_get(owner, "(s)", &state->daemon);
  int status;
  g_autoptr(GVariant) manager = properties(state, NM_PATH, NM, &status);
  gboolean enabled = FALSE, hardware = FALSE;
  if (manager != NULL && (!g_variant_lookup(manager, "WirelessEnabled", "b", &enabled) ||
      !g_variant_lookup(manager, "WirelessHardwareEnabled", "b", &hardware))) status = NXRS_WIFI_PROTOCOL;
  if (status == NXRS_WIFI_OK && (!enabled || !hardware)) status = NXRS_WIFI_UNAVAILABLE;
  if (status != NXRS_WIFI_OK) { release(state); return status; }
  g_autoptr(GVariant) reply = call(state, NM_PATH, NM, "GetDevices", NULL, "(ao)", &status);
  if (reply == NULL) { release(state); return status; }
  g_autoptr(GVariant) devices = g_variant_get_child_value(reply, 0);
  GVariantIter iterator; const char *path;
  g_variant_iter_init(&iterator, devices);
  while (g_variant_iter_next(&iterator, "&o", &path)) {
    g_autoptr(GVariant) device = properties(state, path, DEVICE, &status);
    guint32 type; gboolean managed;
    if (device == NULL) { release(state); return status; }
    if (!g_variant_lookup(device, "DeviceType", "u", &type) ||
        !g_variant_lookup(device, "Managed", "b", &managed)) {
      release(state); return NXRS_WIFI_PROTOCOL;
    }
    if (type != 2 || !managed) continue;
    /* Interface selection belongs to the product platform, never enumeration
     * order. Until that policy exists, refuse ambiguous hardware. */
    if (state->device != NULL) { release(state); return NXRS_WIFI_BUSY; }
    state->device = g_strdup(path);
  }
  if (state->device == NULL) { release(state); return NXRS_WIFI_UNAVAILABLE; }
  *context = state;
  return NXRS_WIFI_OK;
}

static int last_scan(struct wifi_context *state, gint64 *timestamp)
{
  int status;
  g_autoptr(GVariant) wireless = properties(state, state->device, WIRELESS, &status);
  if (wireless == NULL) return status;
  return g_variant_lookup(wireless, "LastScan", "x", timestamp) ? NXRS_WIFI_OK : NXRS_WIFI_PROTOCOL;
}

int nxrs_host_wifi_scan(void *context, struct nxrs_host_wifi_ap *rows,
                        uint32_t capacity, uint32_t *count, uint32_t *truncated)
{
  if (context == NULL || rows == NULL || capacity == 0 || count == NULL || truncated == NULL)
    return NXRS_WIFI_INVALID;
  *count = *truncated = 0;
  struct wifi_context *state = context;
  state->deadline = g_get_monotonic_time() + 10000000;
  gint64 before, after;
  int status = last_scan(state, &before);
  if (status != NXRS_WIFI_OK) return status;
  GVariantBuilder options; g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
  g_autoptr(GVariant) scan = call(state, state->device, WIRELESS, "RequestScan",
    g_variant_new("(a{sv})", &options), "()", &status);
  if (scan == NULL) return status;
  /* Only this blocking worker waits. The service owner retains its one wait
   * point, and stale cached APs cannot masquerade as a newly completed scan. */
  do {
    status = last_scan(state, &after);
    if (status != NXRS_WIFI_OK) return status;
    if (after > before) break;
    g_usleep(20000);
  } while (true);
  g_autoptr(GVariant) reply = call(state, state->device, WIRELESS,
    "GetAllAccessPoints", NULL, "(ao)", &status);
  if (reply == NULL) return status;
  g_autoptr(GVariant) access_points = g_variant_get_child_value(reply, 0);
  GVariantIter iterator; const char *path;
  g_variant_iter_init(&iterator, access_points);
  while (g_variant_iter_next(&iterator, "&o", &path)) {
    if (*count == capacity) { *truncated = 1; break; }
    g_autoptr(GVariant) ap = properties(state, path, AP, &status);
    if (ap == NULL) return status;
    g_autoptr(GVariant) ssid = g_variant_lookup_value(ap, "Ssid", G_VARIANT_TYPE_BYTESTRING);
    guint32 flags, wpa, rsn;
    if (ssid == NULL || !g_variant_lookup(ap, "Flags", "u", &flags) ||
        !g_variant_lookup(ap, "WpaFlags", "u", &wpa) ||
        !g_variant_lookup(ap, "RsnFlags", "u", &rsn)) return NXRS_WIFI_PROTOCOL;
    gsize length; const void *bytes = g_variant_get_fixed_array(ssid, &length, 1);
    if (length > sizeof(rows[0].ssid)) return NXRS_WIFI_PROTOCOL;
    struct nxrs_host_wifi_ap *row = &rows[(*count)++];
    memset(row, 0, sizeof(*row));
    row->ssid_length = (uint32_t)length;
    if (length != 0) memcpy(row->ssid, bytes, length);
    row->security = (rsn & 0x100) != 0 ? 1 : (flags == 0 && wpa == 0 && rsn == 0 ? 0 : 2);
    /* NetworkManager exposes signal quality percent, not measured dBm. */
    row->rssi_dbm = INT32_MIN;
  }
  return NXRS_WIFI_OK;
}

static int addressed(struct wifi_context *state, const char *path, gboolean ipv6,
                     bool *found)
{
  if (strcmp(path, "/") == 0) return NXRS_WIFI_OK;
  int status;
  g_autoptr(GVariant) config = properties(state, path, ipv6 ? NM ".IP6Config" : NM ".IP4Config", &status);
  if (config == NULL) return status;
  g_autoptr(GVariant) addresses = g_variant_lookup_value(config, "AddressData", G_VARIANT_TYPE("aa{sv}"));
  if (addresses == NULL) return NXRS_WIFI_PROTOCOL;
  for (gsize i = 0; i < g_variant_n_children(addresses); i++) {
    g_autoptr(GVariant) data = g_variant_get_child_value(addresses, i);
    const char *text; guint32 prefix;
    if (!g_variant_lookup(data, "address", "&s", &text) ||
        !g_variant_lookup(data, "prefix", "u", &prefix) || prefix > (ipv6 ? 128u : 32u))
      return NXRS_WIFI_PROTOCOL;
    g_autoptr(GInetAddress) address = g_inet_address_new_from_string(text);
    if (address == NULL || g_inet_address_get_family(address) !=
        (ipv6 ? G_SOCKET_FAMILY_IPV6 : G_SOCKET_FAMILY_IPV4)) return NXRS_WIFI_PROTOCOL;
    if (!g_inet_address_get_is_any(address) && !g_inet_address_get_is_loopback(address) &&
        !g_inet_address_get_is_link_local(address) && !g_inet_address_get_is_multicast(address)) {
      *found = true;
      return NXRS_WIFI_OK;
    }
  }
  return NXRS_WIFI_OK;
}

static int link_state(struct wifi_context *state, uint32_t *link)
{
  int status;
  g_autoptr(GVariant) wireless = properties(state, state->device, WIRELESS, &status);
  if (wireless == NULL) return status;
  const char *ap;
  if (!g_variant_lookup(wireless, "ActiveAccessPoint", "&o", &ap)) return NXRS_WIFI_PROTOCOL;
  if (strcmp(ap, "/") == 0) { *link = 1; return NXRS_WIFI_OK; }
  g_autoptr(GVariant) device = properties(state, state->device, DEVICE, &status);
  if (device == NULL) return status;
  guint32 device_state; const char *ipv4, *ipv6;
  if (!g_variant_lookup(device, "State", "u", &device_state) ||
      !g_variant_lookup(device, "Ip4Config", "&o", &ipv4) ||
      !g_variant_lookup(device, "Ip6Config", "&o", &ipv6)) return NXRS_WIFI_PROTOCOL;
  if (state->active != NULL) {
    const char *current;
    if (!g_variant_lookup(device, "ActiveConnection", "&o", &current)) return NXRS_WIFI_PROTOCOL;
    if (strcmp(current, state->active) != 0) { *link = 1; return NXRS_WIFI_OK; }
  }
  bool found = false;
  if (device_state == 100) {
    status = addressed(state, ipv4, false, &found);
    if (status == NXRS_WIFI_OK && !found) status = addressed(state, ipv6, true, &found);
    if (status != NXRS_WIFI_OK) return status;
  }
  *link = found ? 3 : 2;
  return NXRS_WIFI_OK;
}

int nxrs_host_wifi_link(void *context, uint32_t *link)
{
  if (context == NULL || link == NULL) return NXRS_WIFI_INVALID;
  struct wifi_context *state = context;
  state->deadline = g_get_monotonic_time() + 2000000;
  return link_state(state, link);
}

/* Keep the method completion, not just its timeout. Cancelling a GIO wait does
 * not cancel NetworkManager's activation; losing the returned object path
 * would make rollback impossible to prove. Only this worker pumps callbacks. */
static void activated(GObject *object, GAsyncResult *result, gpointer context)
{
  struct wifi_context *state = context;
  state->activation_reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(object),
    result, &state->activation_error);
  state->activating = false;
}

static int settle_activation(struct wifi_context *state)
{
  while (g_main_context_iteration(state->events, FALSE)) {}
  if (state->activating) return NXRS_WIFI_BUSY;
  if (state->activation_error != NULL) {
    int status = bus_error(state->activation_error);
    /* Only side-effect-free admission rejections prove no attempt exists.
     * Even a remote NoReply/Failed is not a rollback acknowledgement. */
    state->uncertain = !g_dbus_error_is_remote_error(state->activation_error) ||
      (status != NXRS_WIFI_UNSUPPORTED && status != NXRS_WIFI_DENIED && status != NXRS_WIFI_INVALID);
    g_clear_error(&state->activation_error);
    return state->uncertain ? NXRS_WIFI_IO : status;
  }
  if (state->activation_reply != NULL) {
    const char *profile, *active;
    g_autoptr(GVariant) options = NULL;
    g_variant_get(state->activation_reply, "(&o&o@a{sv})", &profile, &active, &options);
    if (strcmp(profile, "/") == 0 || strcmp(active, "/") == 0) {
      state->uncertain = true;
    } else {
      state->active = g_strdup(active);
    }
    g_clear_pointer(&state->activation_reply, g_variant_unref);
  }
  return state->uncertain ? NXRS_WIFI_IO : NXRS_WIFI_OK;
}

static int active_state(struct wifi_context *state, guint32 *value)
{
  int status;
  /* Deactivated objects may disappear before their final State is read. Use
   * the pinned daemon's authoritative list, not a swallowed IPC error. */
  g_autoptr(GVariant) manager = properties(state, NM_PATH, NM, &status);
  if (manager == NULL) return status;
  g_autoptr(GVariant) connections = g_variant_lookup_value(manager, "ActiveConnections",
    G_VARIANT_TYPE("ao"));
  if (connections == NULL) return NXRS_WIFI_PROTOCOL;
  GVariantIter iterator; const char *path; bool found = false;
  g_variant_iter_init(&iterator, connections);
  while (g_variant_iter_next(&iterator, "&o", &path))
    if (strcmp(path, state->active) == 0) { found = true; break; }
  if (!found) { *value = 4; return NXRS_WIFI_OK; }
  g_autoptr(GVariant) active = properties(state, state->active, ACTIVE, &status);
  if (active == NULL) return status;
  return g_variant_lookup(active, "State", "u", value) && *value <= 4
    ? NXRS_WIFI_OK : NXRS_WIFI_PROTOCOL;
}

static int deactivate(struct wifi_context *state)
{
  if (state->active == NULL) return NXRS_WIFI_OK;
  int status;
  guint32 value;
  status = active_state(state, &value);
  if (status != NXRS_WIFI_OK) return NXRS_WIFI_IO;
  if (value == 4) { g_clear_pointer(&state->active, g_free); return NXRS_WIFI_OK; }
  g_autoptr(GVariant) reply = call(state, NM_PATH, NM, "DeactivateConnection",
    g_variant_new("(o)", state->active), "()", &status);
  /* A missing or timed-out reply is not a failed rollback if the daemon can
   * independently prove this exact activation is no longer active. */
  do {
    status = active_state(state, &value);
    if (status != NXRS_WIFI_OK) return NXRS_WIFI_IO;
    if (value == 4) {
      g_clear_pointer(&state->active, g_free);
      return NXRS_WIFI_OK;
    }
    g_usleep(20000);
  } while (true);
}

int nxrs_host_wifi_connect(void *context, const uint8_t *ssid, size_t ssid_length,
                          const uint8_t *password, size_t password_length)
{
  if (context == NULL || ssid == NULL || ssid_length == 0 || ssid_length > 32 ||
      (password_length != 0 && (password == NULL || password_length < 8 || password_length > 63)))
    return NXRS_WIFI_INVALID;
  for (size_t i = 0; i < password_length; i++)
    if (password[i] < 0x20 || password[i] > 0x7e) return NXRS_WIFI_INVALID;
  struct wifi_context *state = context;
  if (state->active != NULL || state->activating || state->uncertain) return NXRS_WIFI_BUSY;
  state->deadline = g_get_monotonic_time() + 20000000;
  int status;
  g_autoptr(GVariant) device = properties(state, state->device, DEVICE, &status);
  if (device == NULL) return status;
  const char *current;
  if (!g_variant_lookup(device, "ActiveConnection", "&o", &current)) return NXRS_WIFI_PROTOCOL;
  if (strcmp(current, "/") != 0) return NXRS_WIFI_BUSY;

  GVariantBuilder settings, connection, wireless, security, ipv4, ipv6, options;
  g_variant_builder_init(&settings, G_VARIANT_TYPE("a{sa{sv}}"));
  g_variant_builder_init(&connection, G_VARIANT_TYPE_VARDICT);
  g_autofree char *uuid = g_uuid_string_random();
  g_variant_builder_add(&connection, "{sv}", "id", g_variant_new_string("nxrs-wifi"));
  g_variant_builder_add(&connection, "{sv}", "uuid", g_variant_new_string(uuid));
  g_variant_builder_add(&connection, "{sv}", "type", g_variant_new_string("802-11-wireless"));
  g_variant_builder_add(&connection, "{sv}", "autoconnect", g_variant_new_boolean(FALSE));
  g_variant_builder_add(&settings, "{sa{sv}}", "connection", &connection);
  g_variant_builder_init(&wireless, G_VARIANT_TYPE_VARDICT);
  g_variant_builder_add(&wireless, "{sv}", "ssid", g_variant_new_fixed_array(G_VARIANT_TYPE_BYTE,
    ssid, ssid_length, 1));
  g_variant_builder_add(&wireless, "{sv}", "mode", g_variant_new_string("infrastructure"));
  g_variant_builder_add(&settings, "{sa{sv}}", "802-11-wireless", &wireless);
  if (password_length != 0) {
    g_autofree char *passphrase = g_strndup((const char *)password, password_length);
    const char *protocols[] = {"rsn", NULL};
    g_variant_builder_init(&security, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&security, "{sv}", "key-mgmt", g_variant_new_string("wpa-psk"));
    g_variant_builder_add(&security, "{sv}", "proto", g_variant_new_strv(protocols, -1));
    g_variant_builder_add(&security, "{sv}", "psk", g_variant_new_string(passphrase));
    g_variant_builder_add(&security, "{sv}", "psk-flags", g_variant_new_uint32(2)); /* NOT_SAVED */
    g_variant_builder_add(&settings, "{sa{sv}}", "802-11-wireless-security", &security);
  }
  g_variant_builder_init(&ipv4, G_VARIANT_TYPE_VARDICT);
  g_variant_builder_add(&ipv4, "{sv}", "method", g_variant_new_string("auto"));
  g_variant_builder_add(&settings, "{sa{sv}}", "ipv4", &ipv4);
  g_variant_builder_init(&ipv6, G_VARIANT_TYPE_VARDICT);
  g_variant_builder_add(&ipv6, "{sv}", "method", g_variant_new_string("auto"));
  g_variant_builder_add(&settings, "{sa{sv}}", "ipv6", &ipv6);
  g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
  g_variant_builder_add(&options, "{sv}", "persist", g_variant_new_string("volatile"));
  g_variant_builder_add(&options, "{sv}", "bind-activation", g_variant_new_string("dbus-client"));
  state->activating = true;
  g_main_context_push_thread_default(state->events);
  g_dbus_connection_call(state->bus, state->daemon, NM_PATH, NM, "AddAndActivateConnection2",
    g_variant_new("(a{sa{sv}}ooa{sv})", &settings, state->device, "/", &options),
    G_VARIANT_TYPE("(ooa{sv})"), G_DBUS_CALL_FLAGS_NO_AUTO_START,
    G_MAXINT, NULL, activated, state);
  g_main_context_pop_thread_default(state->events);
  do {
    status = settle_activation(state);
    if (status != NXRS_WIFI_BUSY) break;
    /* Unknown completion is a session fault, never a retryable Timeout. */
    if (g_get_monotonic_time() >= state->deadline) return NXRS_WIFI_IO;
    g_usleep(20000);
  } while (true);
  if (status != NXRS_WIFI_OK) return status;
  do {
    guint32 value, link;
    status = active_state(state, &value);
    if (status != NXRS_WIFI_OK || value == 4) { status = NXRS_WIFI_IO; break; }
    /* Read addressing only after this activation, not another host client's
     * connection. A method reply alone does not mean DHCP completed. */
    g_autoptr(GVariant) current_device = properties(state, state->device, DEVICE, &status);
    if (current_device == NULL) break;
    if (!g_variant_lookup(current_device, "ActiveConnection", "&o", &current)) {
      status = NXRS_WIFI_PROTOCOL; break;
    }
    if (strcmp(current, state->active) != 0) { status = NXRS_WIFI_IO; break; }
    status = link_state(state, &link);
    if (status != NXRS_WIFI_OK) break;
    if (value == 2 && link == 3) return NXRS_WIFI_OK;
    if (g_get_monotonic_time() >= state->deadline) { status = NXRS_WIFI_TIMEOUT; break; }
    g_usleep(20000);
  } while (true);
  state->deadline = g_get_monotonic_time() + 5000000;
  return deactivate(state) == NXRS_WIFI_OK ? status : NXRS_WIFI_IO;
}

int nxrs_host_wifi_disconnect(void *context)
{
  if (context == NULL) return NXRS_WIFI_INVALID;
  struct wifi_context *state = context;
  if (state->active == NULL) return NXRS_WIFI_BUSY;
  state->deadline = g_get_monotonic_time() + 5000000;
  return deactivate(state);
}

int nxrs_host_wifi_release(void *context)
{
  if (context == NULL) return NXRS_WIFI_OK;
  struct wifi_context *state = context;
  int status = settle_activation(state);
  if (state->activating || state->uncertain) return status;
  state->deadline = g_get_monotonic_time() + 5000000;
  status = deactivate(state);
  if (status != NXRS_WIFI_OK) return status;
  release(state);
  return NXRS_WIFI_OK;
}
