/* Exercise the actual patched BCMF function, with native I/O replaced only. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>

#define FAR
#define OK 0
#define IW_ENCODE_ALG_NONE 0
#define IW_ENCODE_ALG_WEP 1
#define IW_ENCODE_ALG_TKIP 2
#define IW_ENCODE_ALG_CCMP 3
#define IW_ENCODE_DISABLED 0x8000
#define WSEC_PASSPHRASE 1
#define wlerr(...) ((void)0)
/* This test checks driver state, not the target libc erasure primitive. */
#define explicit_bzero(pointer, length) ((void)memset(pointer, 0, length))

typedef struct { uint8_t key[64]; uint16_t key_len, flags; } wsec_pmk_t;
struct bcmf_dev_s { bool auth_pending; wsec_pmk_t auth_pmk; };
struct iw_encode_ext { uint16_t alg, key_len; uint8_t key[64]; };
struct iwreq { union { struct { void *pointer; uint16_t flags; } encoding; } u; };
static int bcmf_wl_get_interface(struct bcmf_dev_s *device, struct iwreq *request)
{
  (void)device;
  (void)request;
  return 0;
}

/* Generated from the pinned source after applying the tracked patch. */
#include "bcmf-function.h"

int main(void)
{
  struct bcmf_dev_s device = { .auth_pending = true };
  struct iw_encode_ext key = { .alg = IW_ENCODE_ALG_NONE };
  struct iwreq request = { .u.encoding = { &key, IW_ENCODE_DISABLED } };
  uint8_t zero[sizeof(device.auth_pmk)] = {0};
  memset(&device.auth_pmk, 0x5a, sizeof(device.auth_pmk));
  assert(bcmf_wl_set_encode_ext(&device, &request) == 0);
  assert(!device.auth_pending);
  assert(memcmp(&device.auth_pmk, zero, sizeof(zero)) == 0);

  /* NONE is not an implicit credential-bearing encryption scheme. */
  device.auth_pending = true;
  key.key_len = 8;
  assert(bcmf_wl_set_encode_ext(&device, &request) == -EINVAL);
  assert(device.auth_pending);
  key.key_len = 0;
  request.u.encoding.flags = 0;
  assert(bcmf_wl_set_encode_ext(&device, &request) == -EINVAL);

  /* WPA2 staging is unchanged. Association applies this exactly once. */
  key.alg = IW_ENCODE_ALG_CCMP;
  key.key_len = 8;
  memcpy(key.key, "test-key", 8);
  assert(bcmf_wl_set_encode_ext(&device, &request) == 0);
  assert(device.auth_pending);
  assert(device.auth_pmk.key_len == 8);
  assert(memcmp(device.auth_pmk.key, "test-key", 8) == 0);
  assert(device.auth_pmk.flags == WSEC_PASSPHRASE);
  return 0;
}
