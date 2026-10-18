/* Private BlueZ wire helpers shared by scanning and GATT. */
#ifndef NXRS_BLUEZ_H
#define NXRS_BLUEZ_H
#include <gio/gio.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static inline void bluez_identity(const char *daemon, const char *path, uint8_t output[16])
{
  g_autoptr(GChecksum) checksum = g_checksum_new(G_CHECKSUM_SHA256);
  g_checksum_update(checksum, (const guchar *)daemon, strlen(daemon) + 1);
  g_checksum_update(checksum, (const guchar *)path, strlen(path));
  guint8 digest[32]; gsize size = sizeof(digest);
  g_checksum_get_digest(checksum, digest, &size);
  memcpy(output, digest, 16);
}

static inline void bluez_uuid(const uint8_t bytes[16], char output[37])
{
  snprintf(output, 37,
    "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
    bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
    bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]);
}
#endif
