/* Header-defined GLib constants and opaque GError field access only.
 * Rust owns all D-Bus schemas, calls, callback storage and lifecycle. */
#include <gio/gio.h>
#include <errno.h>
int nxrs_wifi_gio_constant(unsigned kind)
{
  switch(kind){case 0:return G_BUS_TYPE_SYSTEM;
    case 1:return G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT|G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION;
    case 2:return G_DBUS_CALL_FLAGS_NO_AUTO_START;case 3:return G_CHECKSUM_SHA256;
    case 4:return EINVAL;case 5:return EBUSY;case 6:return EIO;case 7:return ENOTCONN;
    case 8:return EMSGSIZE;case 9:return EEXIST;case 10:return ENOENT;case 11:return ENOBUFS;
    case 12:return ENOTSUP;case 13:return EACCES;case 14:return ENETDOWN;case 15:return ECANCELED;
    case 16:return ENODEV;case 17:return EAGAIN;case 18:return ETIMEDOUT;default:return 0;}
}
unsigned nxrs_wifi_gio_error_class(const GError *error)
{
  if(error==NULL)return 0;
  if(g_error_matches(error,G_IO_ERROR,G_IO_ERROR_TIMED_OUT)||g_error_matches(error,G_DBUS_ERROR,G_DBUS_ERROR_NO_REPLY)||g_error_matches(error,G_DBUS_ERROR,G_DBUS_ERROR_TIMEOUT))return 1;
  if(g_error_matches(error,G_DBUS_ERROR,G_DBUS_ERROR_ACCESS_DENIED)||g_error_matches(error,G_DBUS_ERROR,G_DBUS_ERROR_AUTH_FAILED))return 2;
  if(g_error_matches(error,G_DBUS_ERROR,G_DBUS_ERROR_SERVICE_UNKNOWN)||g_error_matches(error,G_DBUS_ERROR,G_DBUS_ERROR_NAME_HAS_NO_OWNER)||g_error_matches(error,G_IO_ERROR,G_IO_ERROR_CLOSED))return 3;
  if(g_error_matches(error,G_DBUS_ERROR,G_DBUS_ERROR_UNKNOWN_METHOD))return 4;
  return 0;
}
char *nxrs_wifi_gio_error_text(const GError *error)
{GError *copy=g_error_copy(error);g_dbus_error_strip_remote_error(copy);char *text=g_strdup(copy->message);g_error_free(copy);return text;}
