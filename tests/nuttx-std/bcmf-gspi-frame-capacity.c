/* SPDX-License-Identifier: MIT */
/* Native regression harness for the actual patched frame allocator. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define begin_packed_struct
#define end_packed_struct __attribute__((packed))
#define wlerr(...) ((void)0)
#define HEADER_SIZE 18
#define MAX_NETDEV_PKTSIZE 1500
#define CONFIG_NET_GUARDSIZE 16
#ifndef TEST_FRAME_CAPACITY
#  define TEST_FRAME_CAPACITY 2048
#endif
#define CONFIG_IEEE80211_BROADCOM_FULLMAC_GSPI_MAX_FRAME TEST_FRAME_CAPACITY
#define CONFIG_IEEE80211_BROADCOM_DMABUF_ALIGNMENT 4
#define FIRST_WORD_SIZE 4
#define aligned_data(n) __attribute__((aligned(n)))

struct bcmf_frame_s
{
  uint8_t *base;
  uint8_t *data;
  unsigned int len;
};

struct bcmf_dev_s
{
  void *bus;
};

struct list_node
{
  struct list_node *next;
  struct list_node *prev;
};

/* Extract the native frame typedef and packed SDPCM header from patched
 * bcmf_interface.h and bcmf_sdpcm.c, respectively.
 */
#include "bcmf-frame-capacity-types.h"

static struct
{
  bcmf_interface_frame_t frame;
  uint8_t canary[16];
} guarded_pool;
#define pool_frame guarded_pool.frame
static unsigned int allocate_calls;
static unsigned int free_calls;
static bool next_alloc_fails;

static bcmf_interface_frame_t *bcmf_interface_allocate_frame(
    struct bcmf_dev_s *priv, bool block, bool tx)
{
  (void)priv;
  (void)block;
  allocate_calls++;
  if (next_alloc_fails)
    {
      return NULL;
    }

  pool_frame.header.base = pool_frame.data;
  pool_frame.header.data = pool_frame.data;
#ifdef TEST_SDIO
  pool_frame.header.len = HEADER_SIZE + MAX_NETDEV_PKTSIZE +
                          CONFIG_NET_GUARDSIZE;
#else
  pool_frame.header.len = CONFIG_IEEE80211_BROADCOM_FULLMAC_GSPI_MAX_FRAME;
#endif
  pool_frame.tx = tx;
  return &pool_frame;
}

static void bcmf_interface_free_frame(struct bcmf_dev_s *priv,
                                      bcmf_interface_frame_t *iframe)
{
  (void)priv;
  assert(iframe == &pool_frame);
  iframe->tx = false;
  free_calls++;
}

#include "bcmf-frame-capacity-functions.h"

static void check_canary(const uint8_t *canary, uint8_t value)
{
  for (size_t i = 0; i < 16; i++)
    {
      assert(canary[i] == value);
    }
}

int main(void)
{
  struct bcmf_dev_s dev = {0};
  const unsigned int header_len = sizeof(struct bcmf_sdpcm_header) + 2;
  const unsigned int capacity =
#ifdef TEST_SDIO
      MAX_NETDEV_PKTSIZE + HEADER_SIZE;
#else
      CONFIG_IEEE80211_BROADCOM_FULLMAC_GSPI_MAX_FRAME;
#endif
  struct bcmf_frame_s *result;

  memset(&guarded_pool, 0xa5, sizeof(guarded_pool));
  memset(&pool_frame, 0, sizeof(pool_frame));

  /* Payload ending exactly at the configured SDPCM frame limit succeeds. */
  result = bcmf_sdpcm_alloc_frame(&dev, capacity - header_len, false, false);
  assert(result == &pool_frame.header);
  assert(result->len == capacity);
  assert(result->data == pool_frame.data + header_len);
  assert(allocate_calls == 1 && free_calls == 0);
  memset(result->data, 0x31, capacity - header_len);
  bcmf_sdpcm_free_frame(&dev, result);
  assert(free_calls == 1 && !pool_frame.tx);
  check_canary(guarded_pool.canary, 0xa5);

#ifndef TEST_SDIO
  /* An unaligned configured maximum still fits its rounded physical slot. */
  {
    const unsigned int rounded =
        (CONFIG_IEEE80211_BROADCOM_FULLMAC_GSPI_MAX_FRAME + 3) & ~3;
    assert(sizeof(pool_frame.data) >= rounded + CONFIG_NET_GUARDSIZE);
    memset(pool_frame.data + CONFIG_IEEE80211_BROADCOM_FULLMAC_GSPI_MAX_FRAME,
           0x3c, rounded - CONFIG_IEEE80211_BROADCOM_FULLMAC_GSPI_MAX_FRAME);
    check_canary(guarded_pool.canary, 0xa5);
  }
  result = bcmf_sdpcm_alloc_frame(&dev,
                                  capacity - header_len - 2, false, false);
  assert(result != NULL && result->len == capacity - 2);
  bcmf_sdpcm_free_frame(&dev, result);
#else
  assert(sizeof(pool_frame.data) == HEADER_SIZE + MAX_NETDEV_PKTSIZE +
                                      CONFIG_NET_GUARDSIZE);
#endif

  /* One byte over capacity is rejected before reserving a pool slot. */
  {
    unsigned int before = allocate_calls;
    result = bcmf_sdpcm_alloc_frame(&dev, capacity - header_len + 1,
                                    false, false);
    assert(result == NULL && allocate_calls == before);
    result = bcmf_sdpcm_alloc_frame(&dev, UINT32_MAX, false, true);
    assert(result == NULL && allocate_calls == before);
  }

  /* CDC control frames reserve the RX pool, without data-frame padding. */
  result = bcmf_sdpcm_alloc_frame(&dev,
      capacity - sizeof(struct bcmf_sdpcm_header), false, true);
  assert(result && result->len == capacity && !pool_frame.tx);
  memset(result->data, 0x42, capacity - sizeof(struct bcmf_sdpcm_header));
  bcmf_sdpcm_free_frame(&dev, result);
  check_canary(guarded_pool.canary, 0xa5);

  /* Pool exhaustion after validation returns cleanly and reserves nothing. */
  next_alloc_fails = true;
  result = bcmf_sdpcm_alloc_frame(&dev, 32, false, false);
  assert(result == NULL && pool_frame.tx == false);
  next_alloc_fails = false;
  check_canary(guarded_pool.canary, 0xa5);
  return 0;
}
