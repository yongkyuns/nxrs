/* SPDX-License-Identifier: MIT */
#include <errno.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <nuttx/mtd/configdata.h>

#if !defined(CONFIG_MTD_CONFIG_NVS) || !defined(CONFIG_MTD_CONFIG_NAMED)
#error "Configuration provider requires named NuttX NVS"
#endif
_Static_assert(CONFIG_MTD_CONFIG_NAME_LEN == 32, "configuration key ABI");

/* Private target-header translations; file ownership and validation are Rust. */
static int status(int ret)
{
  return ret >= 0 ? 0 : errno == ENOENT ? 1 : errno == ENOSPC ? -1 : -2;
}

int config_get(int fd, const uint8_t key[32], uint8_t *data, size_t *length)
{
  struct config_data_s item = { .configdata = data, .len = *length };
  memcpy(item.name, key, sizeof(item.name));
  int ret = status(ioctl(fd, CFGDIOC_GETCONFIG, (unsigned long)&item));
  *length = item.len;
  return ret;
}

int config_set(int fd, const uint8_t key[32], const uint8_t *data, size_t length)
{
  struct config_data_s item = { .configdata = (uint8_t *)data, .len = length };
  memcpy(item.name, key, sizeof(item.name));
  return status(ioctl(fd, CFGDIOC_SETCONFIG, (unsigned long)&item));
}

int config_remove(int fd, const uint8_t key[32])
{
  struct config_data_s item = {0};
  memcpy(item.name, key, sizeof(item.name));
  int ret = status(ioctl(fd, CFGDIOC_DELCONFIG, (unsigned long)&item));
  return ret == 1 ? 0 : ret;
}
