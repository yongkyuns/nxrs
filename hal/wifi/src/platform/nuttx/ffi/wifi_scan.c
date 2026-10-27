/* SPDX-License-Identifier: MIT */

#include <nuttx/config.h>

#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <sys/ioctl.h>
#include <net/if.h>
#include <nuttx/wireless/wireless.h>

/* Private, fixed-width data exchanged with the Rust scan parser. */
struct wifi_scan_field
{
  uint32_t kind;
  uint32_t flags;
  uint32_t length;
  uint8_t data[32];
};

enum wifi_scan_field_kind
{
  NXRS_WIFI_SCAN_IGNORED = 0,
  NXRS_WIFI_SCAN_BSS = 1,
  NXRS_WIFI_SCAN_ESSID = 2,
  NXRS_WIFI_SCAN_QUALITY = 3,
  NXRS_WIFI_SCAN_ENCODING = 4
};

#define NXRS_WIFI_SCAN_LEVEL_VALID (1u << 0)
#define NXRS_WIFI_SCAN_LEVEL_DBM   (1u << 1)
#define NXRS_WIFI_SCAN_ENC_DISABLED (1u << 0)

static int wifi_scan_set_ifname(char destination[IFNAMSIZ],
                                     const char *ifname)
{
  size_t length;

  if (ifname == NULL)
    {
      return EINVAL;
    }

  length = strlen(ifname);
  if (length == 0 || length >= IFNAMSIZ)
    {
      return ENAMETOOLONG;
    }

  memcpy(destination, ifname, length);
  return 0;
}

/* Verify binding/readiness without enabling an interface or starting a scan. */
int wifi_scan_check(int fd, const char *ifname)
{
  struct ifreq interface = {0};
  struct iwreq wireless = {0};
  int result = wifi_scan_set_ifname(interface.ifr_name, ifname);
  if (result != 0)
    {
      return result;
    }

  if (ioctl(fd, SIOCGIFFLAGS, (unsigned long)(uintptr_t)&interface) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  if ((interface.ifr_flags & IFF_UP) == 0)
    {
      return ENETDOWN;
    }

  memcpy(wireless.ifr_name, interface.ifr_name, IFNAMSIZ);
  if (ioctl(fd, SIOCGIWMODE, (unsigned long)(uintptr_t)&wireless) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  return 0;
}

/* The caller brings the interface up and owns fd for its full lifetime. */
int wifi_scan_start(int fd, const char *ifname)
{
  struct iwreq request = {0};
  int result;

  result = wifi_scan_set_ifname(request.ifr_name, ifname);
  if (result != 0)
    {
      return result;
    }

  if (ioctl(fd, SIOCSIWSCAN, (unsigned long)(uintptr_t)&request) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  return 0;
}

int wifi_scan_read(int fd, const char *ifname, uint8_t *buffer,
                        size_t capacity, size_t *length)
{
  struct iwreq request = {0};
  int result;

  if (length == NULL || capacity > UINT16_MAX ||
      (buffer == NULL && capacity != 0))
    {
      return EINVAL;
    }

  result = wifi_scan_set_ifname(request.ifr_name, ifname);
  if (result != 0)
    {
      return result;
    }

  request.u.data.pointer = buffer;
  request.u.data.length = (uint16_t)capacity;
  request.u.data.flags = 0;

  if (ioctl(fd, SIOCGIWSCAN, (unsigned long)(uintptr_t)&request) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  if ((size_t)request.u.data.length > capacity)
    {
      return EOVERFLOW;
    }

  *length = request.u.data.length;
  return 0;
}

/* Decode one native wireless event without assuming stream alignment. */
int wifi_scan_next(const uint8_t *buffer, size_t length, size_t *offset,
                        struct wifi_scan_field *out)
{
  const size_t header_size = offsetof(struct iw_event, u);
  struct wifi_scan_field field = {0};
  size_t current;
  size_t remaining;
  size_t event_length;
  uint16_t native_length;
  uint16_t command;
  const uint8_t *event;

  if (offset == NULL || out == NULL || (buffer == NULL && length != 0))
    {
      return -EINVAL;
    }

  current = *offset;
  if (current > length)
    {
      return -EINVAL;
    }

  if (current == length)
    {
      return 0;
    }

  remaining = length - current;
  if (remaining < header_size)
    {
      return -EINVAL;
    }

  event = buffer + current;
  memcpy(&native_length, event + offsetof(struct iw_event, len),
         sizeof(native_length));
  memcpy(&command, event + offsetof(struct iw_event, cmd), sizeof(command));
  event_length = native_length;

  if (event_length < header_size || event_length > remaining)
    {
      return -EINVAL;
    }

  switch (command)
    {
      case SIOCGIWAP:
        if (event_length < header_size + sizeof(struct sockaddr))
          {
            return -EINVAL;
          }

        field.kind = NXRS_WIFI_SCAN_BSS;
        break;

      case SIOCGIWESSID:
        {
          struct iw_point point;
          size_t payload_length;
          uintptr_t data_offset;

          if (event_length < header_size + sizeof(point))
            {
              return -EINVAL;
            }

          memcpy(&point, event + header_size, sizeof(point));
          payload_length = event_length - header_size;
          data_offset = (uintptr_t)point.pointer;
          if (data_offset < sizeof(point) ||
              data_offset > (uintptr_t)payload_length ||
              (size_t)point.length > payload_length - (size_t)data_offset ||
              point.length > sizeof(field.data))
            {
              return -EINVAL;
            }

          field.kind = NXRS_WIFI_SCAN_ESSID;
          field.length = point.length;
          memcpy(field.data, event + header_size + (size_t)data_offset,
                 point.length);
        }
        break;

      case IWEVQUAL:
        {
          struct iw_quality quality;

          if (event_length < header_size + sizeof(quality))
            {
              return -EINVAL;
            }

          memcpy(&quality, event + header_size, sizeof(quality));
          field.kind = NXRS_WIFI_SCAN_QUALITY;
          field.data[0] = quality.level;
          if ((quality.updated & IW_QUAL_LEVEL_INVALID) == 0)
            {
              field.flags |= NXRS_WIFI_SCAN_LEVEL_VALID;
            }

          if ((quality.updated & IW_QUAL_DBM) != 0)
            {
              field.flags |= NXRS_WIFI_SCAN_LEVEL_DBM;
            }
        }
        break;

      case SIOCGIWENCODE:
        {
          struct iw_point point;

          if (event_length < header_size + sizeof(point))
            {
              return -EINVAL;
            }

          memcpy(&point, event + header_size, sizeof(point));
          field.kind = NXRS_WIFI_SCAN_ENCODING;
          if ((point.flags & IW_ENCODE_DISABLED) != 0)
            {
              field.flags = NXRS_WIFI_SCAN_ENC_DISABLED;
            }
        }
        break;

      default:
        field.kind = NXRS_WIFI_SCAN_IGNORED;
        break;
    }

  *out = field;
  *offset = current + event_length;
  return 1;
}
