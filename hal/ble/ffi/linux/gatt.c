/* BlueZ's blocking IPC stays on one private native executor. Consumer calls
 * only try-lock/copy or post priority controls. No Rust runtime is introduced.
 */
#include <errno.h>
#include <glib-unix.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <sys/eventfd.h>
#include <unistd.h>
#include "../native/nxrs_ble_gatt.h"
#include "bluez.h"

#define B_NAME "org.bluez"
#define B_DEVICE B_NAME ".Device1"
#define B_SERVICE B_NAME ".GattService1"
#define B_CHAR B_NAME ".GattCharacteristic1"
#define B_PROPERTIES "org.freedesktop.DBus.Properties"
#define B_OBJECTS "org.freedesktop.DBus.ObjectManager"
#define B_EVENTS 4
#define B_ATTRIBUTES 32

struct b_reply { GVariant *value; GError *error; bool done; };
struct b_attribute { char *path; bool read, write; };
struct b_backend {
  /* The executor may lock; the consumer never waits for this mutex. */
  uint64_t session;
  uint16_t capacity;
  void (*wake)(void *), (*release)(void *);
  void *arg;
  struct nxrs_gatt_command admitted;
  struct nxrs_gatt_event events[B_EVENTS];
  guint head, count;
  bool requested, busy, closing, poisoned, fault_pending;
  /* Executor only, including all GIO callbacks and their destruction. */
  GMainContext *context;
  GSource *control, *retry;
  GDBusConnection *bus;
  char *daemon, *device;
  guint watches[3], callbacks, attribute_count;
  gulong closed_handler;
  struct b_attribute attributes[B_ATTRIBUTES];
  struct nxrs_gatt_command command;
  struct b_reply stop;
  uint64_t connection, recovery, handled_recovery;
  gint64 stop_retry_at;
  bool owned, gone, removed, resolved, lost, retiring, stopping, stop_again;
};

static pthread_mutex_t b_lock = PTHREAD_MUTEX_INITIALIZER;
static struct b_backend *b_active;
static _Atomic int b_control_fd = -1;
static _Atomic uint64_t b_close_session, b_recover_session, b_recover_operation, b_retry_session;

/* Process-lifetime fd, not a pointer into a session that Drop can free. The
 * counter coalesces notifications; EAGAIN means a wake is already pending. */
static void b_poke(void)
{
  uint64_t one = 1;
  ssize_t result;
  do { result = write(b_control_fd, &one, sizeof(one)); } while (result < 0 && errno == EINTR);
}

static gboolean b_control_ready(gint fd, GIOCondition condition, gpointer unused)
{
  (void)condition; (void)unused;
  uint64_t count;
  while (read(fd, &count, sizeof(count)) < 0 && errno == EINTR) {}
  return G_SOURCE_CONTINUE;
}

static gboolean b_retry_ready(GSource *source, GSourceFunc callback, gpointer data)
{
  (void)callback; (void)data;
  g_source_set_ready_time(source, -1);
  return G_SOURCE_CONTINUE;
}

static void b_closed(GDBusConnection *bus, gboolean vanished, GError *error, gpointer unused)
{
  (void)bus; (void)vanished; (void)error; (void)unused;
  b_poke(); /* May run on GIO's IO thread; retains no backend pointer. */
}

static int b_error(GError *error, uint8_t *att)
{
  if (error == NULL) return EIO;
  if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_TIMED_OUT) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NO_REPLY) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_TIMEOUT)) return ETIMEDOUT;
  if (g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED)) return EACCES;
  if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CLOSED) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER) ||
      g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN)) return ENOTCONN;
  g_autofree char *name = g_dbus_error_get_remote_error(error);
  if (g_strcmp0(name, B_NAME ".Error.NotConnected") == 0) return ENOTCONN;
  if (g_strcmp0(name, B_NAME ".Error.NotReady") == 0) return ENETDOWN;
  if (g_strcmp0(name, B_NAME ".Error.NotAuthorized") == 0 ||
      g_strcmp0(name, B_NAME ".Error.NotPermitted") == 0) return EACCES;
  if (g_strcmp0(name, B_NAME ".Error.NotSupported") == 0) return ENOTSUP;
  if (g_strcmp0(name, B_NAME ".Error.InvalidArguments") == 0 ||
      g_strcmp0(name, B_NAME ".Error.InvalidOffset") == 0 ||
      g_strcmp0(name, B_NAME ".Error.InvalidValueLength") == 0) return EINVAL;
  if (g_strcmp0(name, B_NAME ".Error.InProgress") == 0 ||
      g_strcmp0(name, B_NAME ".Error.AlreadyConnected") == 0) return EBUSY;
  /* BlueZ coalesces some ATT errors into named D-Bus errors. Preserve a raw
   * ATT code only when its Failed reply has this exact upstream wire form;
   * never infer a numeric code from a generic error or prose. */
  if (att != NULL && g_strcmp0(name, B_NAME ".Error.Failed") == 0) {
    g_autoptr(GError) copy = g_error_copy(error);
    g_dbus_error_strip_remote_error(copy);
    const char *prefix = "Operation failed with ATT error: 0x";
    const char *text = copy->message;
    gsize length = strlen(prefix);
    if (strlen(text) == length + 2 && strncmp(text, prefix, length) == 0 &&
        g_ascii_isxdigit(text[length]) && g_ascii_isxdigit(text[length + 1])) {
      unsigned value = g_ascii_xdigit_value(text[length]) * 16 + g_ascii_xdigit_value(text[length + 1]);
      if (value != 0) { *att = value; return 0; }
    }
  }
  return EIO;
}

static void b_refresh(struct b_backend *state)
{
  pthread_mutex_lock(&b_lock);
  if (atomic_load_explicit(&b_close_session, memory_order_acquire) == state->session)
    state->closing = true;
  uint64_t token = atomic_load_explicit(&b_recover_session, memory_order_acquire) == state->session
    ? atomic_load_explicit(&b_recover_operation, memory_order_acquire) : 0;
  if (token != 0 && token != state->handled_recovery &&
      (token == state->command.operation || token == state->connection ||
       (state->requested && token == state->admitted.operation))) {
    state->recovery = state->handled_recovery = token;
    state->retiring = true;
  }
  if (state->closing) state->retiring = true;
  if (state->retiring) { state->requested = false; state->busy = true; }
  pthread_mutex_unlock(&b_lock);
  if (state->bus != NULL && g_dbus_connection_is_closed(state->bus)) state->gone = true;
  if (state->gone || state->removed) {
    if (state->owned && state->connection != 0) state->lost = true;
    state->owned = false;
  }
}

static void b_replied(GObject *object, GAsyncResult *result, gpointer data)
{
  struct b_reply *reply = data;
  reply->value = g_dbus_connection_call_finish(G_DBUS_CONNECTION(object), result, &reply->error);
  reply->done = true;
}

static void b_start_stop(struct b_backend *state)
{
  if (!state->owned || state->stopping || g_get_monotonic_time() < state->stop_retry_at) return;
  state->stopping = true;
  memset(&state->stop, 0, sizeof(state->stop));
  g_dbus_connection_call(state->bus, state->daemon, state->device, B_DEVICE,
    "Disconnect", NULL, G_VARIANT_TYPE("()"), G_DBUS_CALL_FLAGS_NO_AUTO_START,
    G_MAXINT, NULL, b_replied, &state->stop);
}

static void b_collect_stop(struct b_backend *state)
{
  if (!state->stopping || !state->stop.done) return;
  if (state->stop.value != NULL || b_error(state->stop.error, NULL) == ENOTCONN) {
    state->owned = state->stop_again;
    state->stop_again = false;
    if (state->connection != 0) state->lost = true;
  } else {
    state->stop_retry_at = g_get_monotonic_time() + 250000;
    g_source_set_ready_time(state->retry, state->stop_retry_at);
  }
  g_clear_pointer(&state->stop.value, g_variant_unref);
  g_clear_error(&state->stop.error);
  state->stopping = false;
}

static GVariant *b_rpc(struct b_backend *state, const char *path, const char *interface,
                       const char *method, GVariant *parameters, const char *signature,
                       bool mutating, int *error, uint8_t *att, bool *remote)
{
  struct b_reply reply = {0};
  g_dbus_connection_call(state->bus, state->daemon, path, interface, method, parameters,
    G_VARIANT_TYPE(signature), G_DBUS_CALL_FLAGS_NO_AUTO_START, mutating ? G_MAXINT : 2000,
    NULL, b_replied, &reply);
  while (!reply.done) {
    b_refresh(state);
    b_collect_stop(state);
    if (state->retiring) b_start_stop(state);
    if (!reply.done) g_main_context_iteration(state->context, TRUE);
  }
  *error = reply.value != NULL ? 0 : b_error(reply.error, att);
  if (remote != NULL && reply.error != NULL) {
    g_autofree char *name = g_dbus_error_get_remote_error(reply.error);
    *remote = g_strcmp0(name, B_NAME ".Error.NotPermitted") == 0 ||
      g_strcmp0(name, B_NAME ".Error.NotAuthorized") == 0 ||
      g_strcmp0(name, B_NAME ".Error.NotSupported") == 0 ||
      g_strcmp0(name, B_NAME ".Error.InvalidArguments") == 0 ||
      g_strcmp0(name, B_NAME ".Error.InvalidOffset") == 0 ||
      g_strcmp0(name, B_NAME ".Error.InvalidValueLength") == 0 ||
      g_strcmp0(name, B_NAME ".Error.InProgress") == 0;
  }
  g_clear_error(&reply.error);
  return reply.value;
}

static void b_signal(GDBusConnection *bus, const char *sender, const char *path,
                      const char *interface, const char *signal, GVariant *parameters,
                      gpointer data)
{
  (void)bus; (void)sender; (void)interface;
  struct b_backend *state = data;
  if (strcmp(signal, "NameOwnerChanged") == 0) {
    const char *name, *previous, *current;
    if (!g_variant_is_of_type(parameters, G_VARIANT_TYPE("(sss)"))) return;
    g_variant_get(parameters, "(&s&s&s)", &name, &previous, &current);
    if (strcmp(name, state->daemon) == 0 && previous[0] != 0 && current[0] == 0) state->gone = true;
  } else if (strcmp(signal, "InterfacesRemoved") == 0) {
    const char *removed;
    g_autoptr(GVariant) interfaces = NULL;
    if (!g_variant_is_of_type(parameters, G_VARIANT_TYPE("(oas)"))) return;
    g_variant_get(parameters, "(&o@as)", &removed, &interfaces);
    if (g_strcmp0(removed, state->device) == 0 && g_variant_check_format_string(interfaces, "as", FALSE)) {
      GVariantIter list; const char *name; g_variant_iter_init(&list, interfaces);
      while (g_variant_iter_next(&list, "&s", &name)) if (strcmp(name, B_DEVICE) == 0) state->removed = true;
    }
  } else if (g_strcmp0(path, state->device) == 0) {
    const char *name;
    g_autoptr(GVariant) values = NULL, invalidated = NULL;
    if (!g_variant_is_of_type(parameters, G_VARIANT_TYPE("(sa{sv}as)"))) return;
    g_variant_get(parameters, "(&s@a{sv}@as)", &name, &values, &invalidated);
    if (strcmp(name, B_DEVICE) != 0) return;
    gboolean connected, resolved;
    if (g_variant_lookup(values, "Connected", "b", &connected) && !connected && state->owned) {
      state->owned = false;
      if (state->connection != 0) state->lost = true;
    }
    if (g_variant_lookup(values, "ServicesResolved", "b", &resolved)) state->resolved = resolved;
  }
}

static void b_detached(gpointer data) { ((struct b_backend *)data)->callbacks--; }

static int b_bus_open(struct b_backend *state)
{
  g_autoptr(GError) error = NULL;
  g_autofree char *address = g_dbus_address_get_for_bus_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
  if (address == NULL) return b_error(error, NULL);
  state->bus = g_dbus_connection_new_for_address_sync(address,
    G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
    NULL, NULL, &error);
  if (state->bus == NULL) return b_error(error, NULL);
  g_dbus_connection_set_exit_on_close(state->bus, FALSE);
  state->closed_handler = g_signal_connect(state->bus, "closed", G_CALLBACK(b_closed), NULL);
  g_autoptr(GVariant) owner = g_dbus_connection_call_sync(state->bus,
    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
    g_variant_new("(s)", B_NAME), G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NO_AUTO_START, 2000, NULL, &error);
  if (owner == NULL) return b_error(error, NULL);
  g_variant_get(owner, "(s)", &state->daemon);
  state->callbacks = G_N_ELEMENTS(state->watches);
  state->watches[0] = g_dbus_connection_signal_subscribe(state->bus, state->daemon,
    B_PROPERTIES, "PropertiesChanged", NULL, B_DEVICE, G_DBUS_SIGNAL_FLAGS_NONE, b_signal, state, b_detached);
  state->watches[1] = g_dbus_connection_signal_subscribe(state->bus, "org.freedesktop.DBus",
    "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus", state->daemon,
    G_DBUS_SIGNAL_FLAGS_NONE, b_signal, state, b_detached);
  state->watches[2] = g_dbus_connection_signal_subscribe(state->bus, state->daemon,
    B_OBJECTS, "InterfacesRemoved", "/", NULL, G_DBUS_SIGNAL_FLAGS_NONE, b_signal, state, b_detached);
  return 0;
}

static GVariant *b_objects(struct b_backend *state, int *error)
{
  g_autoptr(GVariant) reply = b_rpc(state, "/", B_OBJECTS, "GetManagedObjects", NULL,
    "(a{oa{sa{sv}}})", false, error, NULL, NULL);
  return reply == NULL ? NULL : g_variant_get_child_value(reply, 0);
}

static int b_connect(struct b_backend *state)
{
  if (state->owned || state->device != NULL || state->bus != NULL) return EBUSY;
  int error = b_bus_open(state);
  if (error != 0) return error;
  g_autoptr(GVariant) objects = b_objects(state, &error);
  if (objects == NULL) return error;
  GVariantIter iterator; const char *path; GVariant *interfaces;
  g_variant_iter_init(&iterator, objects);
  while (g_variant_iter_next(&iterator, "{&o@a{sa{sv}}}", &path, &interfaces)) {
    g_autoptr(GVariant) current = interfaces;
    g_autoptr(GVariant) values = g_variant_lookup_value(current, B_DEVICE, G_VARIANT_TYPE_VARDICT);
    if (values == NULL) continue;
    uint8_t identity[16]; bluez_identity(state->daemon, path, identity);
    if (memcmp(identity, state->command.service, 16) != 0) continue;
    gboolean connected;
    if (!g_variant_lookup(values, "Connected", "b", &connected)) return EINVAL;
    if (connected || state->device != NULL) return EBUSY;
    state->device = g_strdup(path);
  }
  if (state->device == NULL) return ENODEV;
  b_refresh(state);
  if (state->retiring) return ECANCELED;
  state->owned = true; /* Keep possible side effects until a real teardown. */
  g_autoptr(GVariant) reply = b_rpc(state, state->device, B_DEVICE, "Connect", NULL,
    "()", true, &error, NULL, NULL);
  if (reply != NULL) {
    state->connection = state->command.operation;
    if (!state->owned) state->lost = true;
    /* A successful Connect reply can arrive after a cancellation's earlier
     * Disconnect reply. Re-own that possibility and require another stop
     * before the drain barrier; an old stop is not proof about a late link. */
    state->owned = true;
    if (state->retiring && state->stopping) state->stop_again = true;
  }
  else if (error == EBUSY || error == EACCES || error == ENETDOWN ||
           error == ENOTSUP || error == EINVAL) state->owned = false;
  return error;
}

static int b_discover(struct b_backend *state, struct nxrs_gatt_event *event)
{
  int error;
  g_autoptr(GVariant) properties = b_rpc(state, state->device, B_PROPERTIES, "GetAll",
    g_variant_new("(s)", B_DEVICE), "(a{sv})", false, &error, NULL, NULL);
  if (properties == NULL) return error;
  g_autoptr(GVariant) values = g_variant_get_child_value(properties, 0);
  gboolean connected, resolved;
  if (!g_variant_lookup(values, "Connected", "b", &connected) ||
      !g_variant_lookup(values, "ServicesResolved", "b", &resolved)) return EINVAL;
  if (!connected) { state->owned = false; return ENOTCONN; }
  state->resolved = resolved;
  while (!state->resolved && !state->retiring && !state->gone && state->owned) {
    g_main_context_iteration(state->context, TRUE); b_refresh(state);
  }
  if (state->retiring) return ECANCELED;
  if (!state->owned || state->gone) return ENOTCONN;
  g_autoptr(GVariant) objects = b_objects(state, &error);
  if (objects == NULL) return error;
  char service[37], characteristic[37];
  bluez_uuid(state->command.service, service); bluez_uuid(state->command.characteristic, characteristic);
  g_autofree char *selected = NULL;
  GVariantIter iterator; const char *path; GVariant *interfaces;
  g_variant_iter_init(&iterator, objects);
  while (g_variant_iter_next(&iterator, "{&o@a{sa{sv}}}", &path, &interfaces)) {
    g_autoptr(GVariant) current = interfaces;
    g_autoptr(GVariant) properties = g_variant_lookup_value(current, B_SERVICE, G_VARIANT_TYPE_VARDICT);
    const char *uuid, *device;
    if (properties == NULL || !g_variant_lookup(properties, "UUID", "&s", &uuid) ||
        !g_variant_lookup(properties, "Device", "&o", &device)) continue;
    if (strcmp(device, state->device) != 0 || g_ascii_strcasecmp(uuid, service) != 0) continue;
    if (selected != NULL) return EEXIST;
    selected = g_strdup(path);
  }
  if (selected == NULL) return ENOENT;
  const char *found = NULL; bool readable = false, writable = false;
  g_variant_iter_init(&iterator, objects);
  while (g_variant_iter_next(&iterator, "{&o@a{sa{sv}}}", &path, &interfaces)) {
    g_autoptr(GVariant) current = interfaces;
    g_autoptr(GVariant) properties = g_variant_lookup_value(current, B_CHAR, G_VARIANT_TYPE_VARDICT);
    const char *uuid, *parent; gboolean notifying = FALSE;
    if (properties == NULL || !g_variant_lookup(properties, "UUID", "&s", &uuid) ||
        !g_variant_lookup(properties, "Service", "&o", &parent)) continue;
    if (strcmp(parent, selected) != 0 || g_ascii_strcasecmp(uuid, characteristic) != 0) continue;
    if (found != NULL) return EEXIST;
    g_variant_lookup(properties, "Notifying", "b", &notifying);
    if (notifying) return ENOTSUP;
    found = path;
    g_autoptr(GVariant) flags = g_variant_lookup_value(properties, "Flags", G_VARIANT_TYPE_STRING_ARRAY);
    if (flags == NULL) return EINVAL;
    GVariantIter list; const char *flag; g_variant_iter_init(&list, flags);
    while (g_variant_iter_next(&list, "&s", &flag)) {
      readable |= strcmp(flag, "read") == 0; writable |= strcmp(flag, "write") == 0;
    }
  }
  if (found == NULL) return ENOENT;
  guint index = 0;
  while (index < state->attribute_count && strcmp(state->attributes[index].path, found) != 0) index++;
  if (index == state->attribute_count) {
    if (index == B_ATTRIBUTES) return ENOBUFS;
    state->attributes[index] = (struct b_attribute){g_strdup(found), readable, writable};
    state->attribute_count++;
  }
  event->handle = index + 1;
  return 0;
}

static void b_execute(struct b_backend *state, struct nxrs_gatt_event *event)
{
  memset(event, 0, sizeof(*event));
  event->operation = state->command.operation; event->kind = state->command.kind;
  if (event->kind == NXRS_GATT_CONNECT) { event->error = b_connect(state); return; }
  if (!state->owned || state->gone || state->connection == 0) { event->error = ENOTCONN; return; }
  if (event->kind == NXRS_GATT_DISCONNECT) {
    state->retiring = true;
    return;
  }
  if (event->kind == NXRS_GATT_DISCOVER) { event->error = b_discover(state, event); return; }
  guint index = state->command.handle;
  if (index == 0 || index > state->attribute_count) { event->error = EINVAL; return; }
  struct b_attribute *attribute = &state->attributes[index - 1];
  bool read = event->kind == NXRS_GATT_READ;
  if (!(read ? attribute->read : attribute->write)) { event->error = ENOTSUP; return; }
  GVariantBuilder options; g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
  if (!read) g_variant_builder_add(&options, "{sv}", "type", g_variant_new_string("request"));
  GVariant *parameters = read ? g_variant_new("(a{sv})", &options) :
    g_variant_new("(@aya{sv})", g_variant_new_fixed_array(G_VARIANT_TYPE_BYTE,
      state->command.data, state->command.length, 1), &options);
  bool remote = false;
  g_autoptr(GVariant) reply = b_rpc(state, attribute->path, B_CHAR,
    read ? "ReadValue" : "WriteValue", parameters, read ? "(ay)" : "()", !read,
    &event->error, &event->att_error, &remote);
  if (remote) event->kind = NXRS_GATT_REMOTE_ERROR;
  if (read && reply != NULL) {
    g_autoptr(GVariant) bytes = g_variant_get_child_value(reply, 0);
    gsize length; const void *data = g_variant_get_fixed_array(bytes, &length, 1);
    if (length > state->capacity) { event->error = EINVAL; return; }
    event->length = length;
    if (length != 0) memcpy(event->data, data, length);
  }
}

static bool b_enqueue(struct b_backend *state, struct nxrs_gatt_event event, bool release_busy)
{
  pthread_mutex_lock(&b_lock);
  bool room = state->count < B_EVENTS;
  if (room) {
    state->events[(state->head + state->count++) % B_EVENTS] = event;
    if (release_busy) state->busy = false;
  } else { state->poisoned = state->fault_pending = true; }
  pthread_mutex_unlock(&b_lock);
  state->wake(state->arg);
  return room;
}

static void b_loss(struct b_backend *state)
{
  if (!state->lost) return;
  struct nxrs_gatt_event event = {.operation = state->connection, .kind = NXRS_GATT_LINK_LOST_UNKNOWN_REASON};
  if (b_enqueue(state, event, false)) state->lost = false;
}

static void b_detach(struct b_backend *state)
{
  for (guint i = 0; i < G_N_ELEMENTS(state->watches); i++) {
    if (state->watches[i] != 0) g_dbus_connection_signal_unsubscribe(state->bus, state->watches[i]);
    state->watches[i] = 0;
  }
  while (state->callbacks != 0) g_main_context_iteration(state->context, TRUE);
  if (state->bus != NULL) {
    if (state->closed_handler != 0) g_signal_handler_disconnect(state->bus, state->closed_handler);
    g_dbus_connection_close_sync(state->bus, NULL, NULL);
    g_clear_object(&state->bus);
  }
  g_clear_pointer(&state->device, g_free); g_clear_pointer(&state->daemon, g_free);
  for (guint i = 0; i < state->attribute_count; i++) g_clear_pointer(&state->attributes[i].path, g_free);
  state->attribute_count = 0; state->closed_handler = 0;
  state->stop_retry_at = 0;
  state->gone = state->removed = state->resolved = false;
}

static gpointer b_worker(gpointer data)
{
  struct b_backend *state = data;
  g_main_context_push_thread_default(state->context);
  state->control = g_unix_fd_source_new(b_control_fd, G_IO_IN);
  g_source_set_callback(state->control, G_SOURCE_FUNC(b_control_ready), NULL, NULL);
  g_source_attach(state->control, state->context);
  static GSourceFuncs retry_funcs = {.dispatch = b_retry_ready};
  state->retry = g_source_new(&retry_funcs, sizeof(GSource));
  g_source_attach(state->retry, state->context);
  for (;;) {
    b_refresh(state); b_collect_stop(state);
    if (state->retiring) {
      if (state->owned && !state->stopping) {
        b_start_stop(state);
      }
      if (!state->owned && !state->stopping) {
        b_loss(state); b_detach(state);
        if (state->closing) break;
        struct nxrs_gatt_event event = {
          .operation = state->recovery != 0 ? state->recovery : state->command.operation,
          .kind = state->recovery != 0 ? NXRS_GATT_QUIESCENT : NXRS_GATT_DISCONNECT};
        if (b_enqueue(state, event, true)) {
          state->retiring = false;
          state->connection = state->recovery = 0;
        }
      }
    } else {
      pthread_mutex_lock(&b_lock);
      bool requested = state->requested;
      if (requested) { state->command = state->admitted; state->requested = false; }
      bool waiting = state->count != 0 || state->fault_pending ||
        atomic_exchange_explicit(&b_retry_session, 0, memory_order_acq_rel) == state->session;
      pthread_mutex_unlock(&b_lock);
      if (requested) {
        struct nxrs_gatt_event event; b_execute(state, &event); b_refresh(state);
        if (state->retiring) continue;
        b_enqueue(state, event, true);
      }
      b_loss(state);
      if (waiting) state->wake(state->arg);
    }
    g_main_context_iteration(state->context, TRUE);
  }
  g_source_destroy(state->control); g_source_unref(state->control);
  g_source_destroy(state->retry); g_source_unref(state->retry);
  g_main_context_pop_thread_default(state->context);
  g_main_context_unref(state->context);
  pthread_mutex_lock(&b_lock); b_active = NULL; pthread_mutex_unlock(&b_lock);
  state->release(state->arg); g_free(state);
  return NULL;
}

int nxrs_ble_gatt_open(uint64_t session, uint16_t capacity, void (*wake)(void *), void (*release)(void *), void *arg)
{
  if (session == 0 || capacity > NXRS_GATT_CAPACITY || wake == NULL || release == NULL) return EINVAL;
  if (pthread_mutex_trylock(&b_lock) != 0) return EBUSY;
  if (b_active != NULL) { pthread_mutex_unlock(&b_lock); return EBUSY; }
  if (b_control_fd < 0) b_control_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
  if (b_control_fd < 0) { int error = errno; pthread_mutex_unlock(&b_lock); return error; }
  struct b_backend *state = g_new0(struct b_backend, 1);
  state->session = session; state->capacity = capacity;
  state->wake = wake; state->release = release; state->arg = arg;
  state->context = g_main_context_new();
  b_active = state;
  GThread *worker = g_thread_try_new("ble-gatt", b_worker, state, NULL);
  if (worker == NULL) {
    b_active = NULL; g_main_context_unref(state->context); g_free(state);
    pthread_mutex_unlock(&b_lock); return EAGAIN;
  }
  g_thread_unref(worker);
  pthread_mutex_unlock(&b_lock);
  return 0;
}

int nxrs_ble_gatt_submit(uint64_t session, const struct nxrs_gatt_command *command)
{
  if (command == NULL || command->operation == 0 || command->kind < NXRS_GATT_CONNECT ||
      command->kind > NXRS_GATT_DISCONNECT || command->length > NXRS_GATT_CAPACITY) return EINVAL;
  if (pthread_mutex_trylock(&b_lock) != 0) {
    atomic_store_explicit(&b_retry_session, session, memory_order_release);
    b_poke(); return EBUSY;
  }
  struct b_backend *state = b_active;
  int error = state == NULL || state->session != session || state->closing ? ENOTCONN :
    state->poisoned ? EIO : state->busy || state->requested ? EBUSY : 0;
  if (error == 0) {
    if (command->length > state->capacity) error = EMSGSIZE;
    else { state->admitted = *command; state->requested = state->busy = true; }
  }
  pthread_mutex_unlock(&b_lock);
  if (error == 0) b_poke();
  return error;
}

void nxrs_ble_gatt_recover(uint64_t session, uint64_t operation)
{
  atomic_store_explicit(&b_recover_operation, operation, memory_order_release);
  atomic_store_explicit(&b_recover_session, session, memory_order_release); b_poke();
}

int nxrs_ble_gatt_poll(uint64_t session, struct nxrs_gatt_event *event)
{
  if (event == NULL) return 0;
  if (pthread_mutex_trylock(&b_lock) != 0) {
    atomic_store_explicit(&b_retry_session, session, memory_order_release);
    b_poke(); return 0;
  }
  struct b_backend *state = b_active;
  int found = state != NULL && state->session == session && (state->count != 0 || state->fault_pending);
  if (found) {
    if (state->fault_pending) { memset(event, 0, sizeof(*event)); state->fault_pending = false; }
    else { *event = state->events[state->head]; state->head = (state->head + 1) % B_EVENTS; state->count--; }
  }
  bool retry = found && state->poisoned;
  pthread_mutex_unlock(&b_lock);
  if (retry) b_poke();
  return found;
}

void nxrs_ble_gatt_close(uint64_t session)
{
  atomic_store_explicit(&b_close_session, session, memory_order_release); b_poke();
}
