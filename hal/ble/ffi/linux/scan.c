/* BlueZ on the shared scan worker. No adapter power, pairing or cache changes.
 * Fresh report signals identify candidates; cached objects alone publish none.
 */
#include <gio/gio.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "../native/scan.h"
#include "bluez.h"

#define BLUEZ "org.bluez"
#define ADAPTER BLUEZ ".Adapter1"
#define DEVICE BLUEZ ".Device1"
#define PROPERTIES "org.freedesktop.DBus.Properties"
#define OBJECTS "org.freedesktop.DBus.ObjectManager"
#define PENDING_REPORTS 32
#define PATH_BYTES 256

struct scan_context {
  GDBusConnection *bus;
  GMainContext *events;
  char *daemon, *adapter;
  guint subscriptions[3], callbacks;
  char paths[PENDING_REPORTS][PATH_BYTES];
  guint count;
  char service[37];
  bool scanning, owned, daemon_gone;
  gint64 deadline;
};

static int bus_error(GError *error)
{
  if (error == NULL) return NXRS_BLE_IO;
  if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NO_REPLY) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_TIMEOUT)) return NXRS_BLE_TIMEOUT;
  if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_AUTH_FAILED)) return NXRS_BLE_DENIED;
  if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER) ||
      g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CLOSED)) return NXRS_BLE_UNAVAILABLE;
  g_autofree char *name = g_dbus_error_get_remote_error(error);
  if (g_strcmp0(name, BLUEZ ".Error.NotAuthorized") == 0 ||
      g_strcmp0(name, BLUEZ ".Error.NotPermitted") == 0) return NXRS_BLE_DENIED;
  if (g_strcmp0(name, BLUEZ ".Error.NotReady") == 0) return NXRS_BLE_UNAVAILABLE;
  if (g_strcmp0(name, BLUEZ ".Error.InProgress") == 0) return NXRS_BLE_BUSY;
  if (g_strcmp0(name, BLUEZ ".Error.NotSupported") == 0) return NXRS_BLE_UNSUPPORTED;
  if (g_strcmp0(name, BLUEZ ".Error.InvalidArguments") == 0) return NXRS_BLE_INVALID;
  return NXRS_BLE_IO;
}

static GVariant *call(struct scan_context *state, const char *path,
                      const char *interface, const char *method,
                      GVariant *parameters, const char *signature, int *status)
{
  gint64 left = state->deadline - g_get_monotonic_time();
  if (left <= 0) {
    if (parameters != NULL) g_variant_unref(g_variant_ref_sink(parameters));
    *status = NXRS_BLE_TIMEOUT;
    return NULL;
  }
  g_autoptr(GError) error = NULL;
  GVariant *reply = g_dbus_connection_call_sync(state->bus, state->daemon, path,
    interface, method, parameters, G_VARIANT_TYPE(signature),
    G_DBUS_CALL_FLAGS_NO_AUTO_START, (gint)MIN((left + 999) / 1000, 2000), NULL, &error);
  *status = reply == NULL ? bus_error(error) : NXRS_BLE_OK;
  return reply;
}

static GVariant *properties(struct scan_context *state, const char *path,
                            const char *interface, int *status)
{
  g_autoptr(GVariant) reply = call(state, path, PROPERTIES, "GetAll",
    g_variant_new("(s)", interface), "(a{sv})", status);
  return reply == NULL ? NULL : g_variant_get_child_value(reply, 0);
}

static bool report_fields(GVariant *values)
{
  const char *keys[] = {"RSSI", "ManufacturerData", "ServiceData", "AdvertisingData"};
  for (guint i = 0; i < G_N_ELEMENTS(keys); i++) {
    g_autoptr(GVariant) value = g_variant_lookup_value(values, keys[i], NULL);
    if (value != NULL) return true;
  }
  return false;
}

static void queue_report(struct scan_context *state, const char *path)
{
  if (!state->scanning || strlen(path) >= PATH_BYTES) return;
  for (guint i = 0; i < state->count; i++)
    if (strcmp(state->paths[i], path) == 0) return;
  /* RF reports are best effort; never overwrite another pending identity. */
  if (state->count < PENDING_REPORTS)
    g_strlcpy(state->paths[state->count++], path, PATH_BYTES);
}

static void report(GDBusConnection *bus, const char *sender, const char *path,
                    const char *interface, const char *signal, GVariant *parameters,
                    gpointer data)
{
  (void)bus; (void)sender; (void)interface;
  struct scan_context *state = data;
  if (!state->scanning) return;
  if (strcmp(signal, "InterfacesAdded") == 0) {
    if (!g_variant_is_of_type(parameters, G_VARIANT_TYPE("(oa{sa{sv}})"))) return;
    const char *device;
    g_autoptr(GVariant) interfaces = NULL;
    g_variant_get(parameters, "(&o@a{sa{sv}})", &device, &interfaces);
    g_autoptr(GVariant) values = g_variant_lookup_value(interfaces, DEVICE, G_VARIANT_TYPE_VARDICT);
    if (values != NULL && report_fields(values)) queue_report(state, device);
  } else {
    if (!g_variant_is_of_type(parameters, G_VARIANT_TYPE("(sa{sv}as)"))) return;
    const char *name;
    g_autoptr(GVariant) values = NULL;
    g_autoptr(GVariant) invalidated = NULL;
    g_variant_get(parameters, "(&s@a{sv}@as)", &name, &values, &invalidated);
    if (strcmp(name, DEVICE) == 0 && report_fields(values)) queue_report(state, path);
  }
}

static void owner_changed(GDBusConnection *bus, const char *sender, const char *path,
                           const char *interface, const char *signal,
                           GVariant *parameters, gpointer data)
{
  (void)bus; (void)sender; (void)path; (void)interface; (void)signal;
  struct scan_context *state = data;
  if (!g_variant_is_of_type(parameters, G_VARIANT_TYPE("(sss)"))) return;
  const char *name, *previous, *current;
  g_variant_get(parameters, "(&s&s&s)", &name, &previous, &current);
  if (strcmp(name, state->daemon) == 0 && previous[0] != 0 && current[0] == 0)
    state->daemon_gone = true;
}

static void detached(gpointer data)
{
  struct scan_context *state = data;
  state->callbacks--;
}

static void detach(struct scan_context *state)
{
  state->scanning = false;
  for (guint i = 0; i < G_N_ELEMENTS(state->subscriptions); i++) {
    if (state->subscriptions[i] != 0)
      g_dbus_connection_signal_unsubscribe(state->bus, state->subscriptions[i]);
    state->subscriptions[i] = 0;
  }
  /* Signal user-data destruction is a separate callback-lifetime barrier. */
  while (state->callbacks != 0) {
    g_main_context_iteration(state->events, FALSE);
    g_usleep(1000);
  }
  state->count = 0;
}

void nxrs_host_ble_scan_release(void *context)
{
  if (context == NULL) return;
  struct scan_context *state = context;
  /* A failed start must not abandon a possibly acquired discovery session.
   * Only this private worker waits/retries; the owner remains asynchronous. */
  while (state->owned && nxrs_host_ble_scan_stop(state) != NXRS_BLE_OK) g_usleep(250000);
  detach(state);
  if (state->bus != NULL) {
    g_dbus_connection_close_sync(state->bus, NULL, NULL);
    g_object_unref(state->bus);
  }
  g_main_context_unref(state->events);
  g_free(state->daemon); g_free(state->adapter); g_free(state);
}

int nxrs_host_ble_scan_open(void **context)
{
  if (context == NULL) return NXRS_BLE_INVALID;
  *context = NULL;
  struct scan_context *state = g_new0(struct scan_context, 1);
  state->events = g_main_context_new();
  state->deadline = g_get_monotonic_time() + 5000000;
  g_autoptr(GError) error = NULL;
  g_autofree char *address = g_dbus_address_get_for_bus_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
  if (address == NULL) { int status = bus_error(error); nxrs_host_ble_scan_release(state); return status; }
  state->bus = g_dbus_connection_new_for_address_sync(address,
    G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
    NULL, NULL, &error);
  if (state->bus == NULL) { int status = bus_error(error); nxrs_host_ble_scan_release(state); return status; }
  g_dbus_connection_set_exit_on_close(state->bus, FALSE);
  g_autoptr(GVariant) owner = g_dbus_connection_call_sync(state->bus,
    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
    "GetNameOwner", g_variant_new("(s)", BLUEZ), G_VARIANT_TYPE("(s)"),
    G_DBUS_CALL_FLAGS_NO_AUTO_START, 2000, NULL, &error);
  if (owner == NULL) { int status = bus_error(error); nxrs_host_ble_scan_release(state); return status; }
  g_variant_get(owner, "(s)", &state->daemon);
  int status;
  g_autoptr(GVariant) reply = call(state, "/", OBJECTS, "GetManagedObjects", NULL,
    "(a{oa{sa{sv}}})", &status);
  if (reply == NULL) { nxrs_host_ble_scan_release(state); return status; }
  g_autoptr(GVariant) objects = g_variant_get_child_value(reply, 0);
  GVariantIter iterator; const char *path; GVariant *interfaces;
  g_variant_iter_init(&iterator, objects);
  while (g_variant_iter_next(&iterator, "{&o@a{sa{sv}}}", &path, &interfaces)) {
    g_autoptr(GVariant) current = interfaces;
    g_autoptr(GVariant) adapter = g_variant_lookup_value(current, ADAPTER, G_VARIANT_TYPE_VARDICT);
    if (adapter == NULL) continue;
    if (state->adapter != NULL) { nxrs_host_ble_scan_release(state); return NXRS_BLE_BUSY; }
    state->adapter = g_strdup(path);
  }
  if (state->adapter == NULL) { nxrs_host_ble_scan_release(state); return NXRS_BLE_UNAVAILABLE; }
  g_autoptr(GVariant) adapter = properties(state, state->adapter, ADAPTER, &status);
  gboolean powered = FALSE;
  if (adapter != NULL && !g_variant_lookup(adapter, "Powered", "b", &powered)) status = NXRS_BLE_INVALID;
  if (status == NXRS_BLE_OK && !powered) status = NXRS_BLE_UNAVAILABLE;
  if (status != NXRS_BLE_OK) { nxrs_host_ble_scan_release(state); return status; }
  *context = state;
  return NXRS_BLE_OK;
}

struct start_reply { GVariant *value; GError *error; bool done; };
static void started(GObject *object, GAsyncResult *result, gpointer data)
{
  struct start_reply *reply = data;
  reply->value = g_dbus_connection_call_finish(G_DBUS_CONNECTION(object), result, &reply->error);
  reply->done = true;
}

int nxrs_host_ble_scan_start(void *context, const uint8_t *service)
{
  if (context == NULL) return NXRS_BLE_INVALID;
  struct scan_context *state = context;
  if (state->owned) return NXRS_BLE_BUSY;
  state->service[0] = 0;
  if (service != NULL) bluez_uuid(service, state->service);
  GVariantBuilder filters; g_variant_builder_init(&filters, G_VARIANT_TYPE_VARDICT);
  const char *uuid = state->service;
  g_variant_builder_add(&filters, "{sv}", "Transport", g_variant_new_string("le"));
  g_variant_builder_add(&filters, "{sv}", "DuplicateData", g_variant_new_boolean(TRUE));
  g_variant_builder_add(&filters, "{sv}", "UUIDs", g_variant_new_strv(&uuid, service == NULL ? 0 : 1));
  int status; state->deadline = g_get_monotonic_time() + 5000000;
  g_autoptr(GVariant) filter = call(state, state->adapter, ADAPTER, "SetDiscoveryFilter",
    g_variant_new("(a{sv})", &filters), "()", &status);
  if (filter == NULL) return status;
  struct start_reply reply = {0};
  g_main_context_push_thread_default(state->events);
  state->callbacks = G_N_ELEMENTS(state->subscriptions);
  state->subscriptions[0] = g_dbus_connection_signal_subscribe(state->bus, state->daemon,
    OBJECTS, "InterfacesAdded", "/", NULL, G_DBUS_SIGNAL_FLAGS_NONE, report, state, detached);
  state->subscriptions[1] = g_dbus_connection_signal_subscribe(state->bus, state->daemon,
    PROPERTIES, "PropertiesChanged", NULL, DEVICE, G_DBUS_SIGNAL_FLAGS_NONE, report, state, detached);
  state->subscriptions[2] = g_dbus_connection_signal_subscribe(state->bus, "org.freedesktop.DBus",
    "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus", state->daemon,
    G_DBUS_SIGNAL_FLAGS_NONE, owner_changed, state, detached);
  state->scanning = true;
  g_dbus_connection_call(state->bus, state->daemon, state->adapter, ADAPTER,
    "StartDiscovery", NULL, G_VARIANT_TYPE("()"), G_DBUS_CALL_FLAGS_NO_AUTO_START,
    G_MAXINT, NULL, started, &reply);
  g_main_context_pop_thread_default(state->events);
  /* An observation timeout cannot cancel a server-side mutating request.
   * Keep the worker/lease until the real reply and any rollback are complete. */
  while (!reply.done) {
    g_main_context_iteration(state->events, FALSE);
    g_usleep(2000);
  }
  status = reply.value != NULL ? NXRS_BLE_OK : bus_error(reply.error);
  if (reply.value != NULL) state->owned = true;
  if (reply.error != NULL && (!g_dbus_error_is_remote_error(reply.error) ||
      status == NXRS_BLE_TIMEOUT)) state->owned = true;
  g_clear_pointer(&reply.value, g_variant_unref); g_clear_error(&reply.error);
  if (status == NXRS_BLE_OK && g_get_monotonic_time() >= state->deadline) status = NXRS_BLE_TIMEOUT;
  if (status != NXRS_BLE_OK) {
    while (state->owned && nxrs_host_ble_scan_stop(state) != NXRS_BLE_OK) g_usleep(250000);
    detach(state);
  }
  return status;
}

int nxrs_host_ble_scan_get(void *context, struct nxrs_host_ble_report *rows,
                           uint32_t capacity, uint32_t *count)
{
  if (context == NULL || rows == NULL || capacity == 0 || count == NULL) return NXRS_BLE_INVALID;
  *count = 0;
  struct scan_context *state = context;
  if (!state->owned || !state->scanning) return NXRS_BLE_UNAVAILABLE;
  if (g_dbus_connection_is_closed(state->bus)) return NXRS_BLE_UNAVAILABLE;
  for (guint i = 0; i < 64 && g_main_context_pending(state->events); i++)
    g_main_context_iteration(state->events, FALSE);
  if (state->daemon_gone) return NXRS_BLE_UNAVAILABLE;
  state->deadline = g_get_monotonic_time() + 2000000;
  guint pending = MIN(state->count, capacity);
  for (guint i = 0; i < pending; i++) {
    int status;
    g_autoptr(GVariant) values = properties(state, state->paths[i], DEVICE, &status);
    if (values == NULL) return status;
    const char *adapter;
    if (!g_variant_lookup(values, "Adapter", "&o", &adapter)) return NXRS_BLE_INVALID;
    if (strcmp(adapter, state->adapter) != 0) continue;
    bool matched = state->service[0] == 0;
    g_autoptr(GVariant) uuids = g_variant_lookup_value(values, "UUIDs", G_VARIANT_TYPE_STRING_ARRAY);
    if (!matched && uuids != NULL) {
      GVariantIter list; const char *uuid; g_variant_iter_init(&list, uuids);
      while (g_variant_iter_next(&list, "&s", &uuid))
        if (g_ascii_strcasecmp(uuid, state->service) == 0) matched = true;
    }
    if (!matched) continue;
    struct nxrs_host_ble_report *row = &rows[(*count)++];
    memset(row, 0, sizeof(*row));
    /* A daemon-scoped opaque OS-path identity, never a fabricated MAC. A new
     * daemon cannot reuse the old token even if it reuses an object path. */
    bluez_identity(state->daemon, state->paths[i], row->peer);
    row->rssi_dbm = INT32_MIN;
    g_autoptr(GVariant) rssi = g_variant_lookup_value(values, "RSSI", NULL);
    if (rssi != NULL) {
      if (!g_variant_is_of_type(rssi, G_VARIANT_TYPE_INT16)) return NXRS_BLE_INVALID;
      gint16 value = g_variant_get_int16(rssi);
      if (value != 127) row->rssi_dbm = value;
    }
    row->matches_filter = 1;
  }
  state->count -= pending;
  memmove(state->paths, state->paths + pending, state->count * PATH_BYTES);
  return NXRS_BLE_OK;
}

int nxrs_host_ble_scan_stop(void *context)
{
  if (context == NULL) return NXRS_BLE_INVALID;
  struct scan_context *state = context;
  if (state->daemon_gone || g_dbus_connection_is_closed(state->bus)) state->owned = false;
  if (!state->owned) { detach(state); return NXRS_BLE_OK; }
  int status; state->deadline = g_get_monotonic_time() + 2000000;
  g_autoptr(GVariant) reply = call(state, state->adapter, ADAPTER, "StopDiscovery", NULL, "()", &status);
  if (reply == NULL) {
    /* A dead unique owner has no discovery session left to stop. This also
     * covers loss between the last get() and this call, without waiting for
     * a queued NameOwnerChanged callback. A replaced well-known name alone
     * is not proof: an original unique owner can still be alive. */
    g_autoptr(GVariant) owner = g_dbus_connection_call_sync(state->bus,
      "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
      "NameHasOwner", g_variant_new("(s)", state->daemon), G_VARIANT_TYPE("(b)"),
      G_DBUS_CALL_FLAGS_NO_AUTO_START, 2000, NULL, NULL);
    gboolean present = TRUE;
    if (owner != NULL) g_variant_get(owner, "(b)", &present);
    if (present && !g_dbus_connection_is_closed(state->bus)) return status;
  }
  state->owned = false;
  detach(state);
  return NXRS_BLE_OK;
}
