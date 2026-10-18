/* Native regression witness for the extracted bcmf_transfer_bytes(). */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define FAR
#define OK 0
#define DEBUGASSERT(condition) assert(condition)

typedef struct gspi_dev_s gspi_dev_t;
struct gspi_dev_s
{
  int (*write)(gspi_dev_t *, bool, uint8_t, uint32_t, uint16_t, uint32_t *);
  int (*read)(gspi_dev_t *, bool, uint8_t, uint32_t, uint16_t, uint32_t *);
};

typedef struct
{
  gspi_dev_t *gspi;
} bcmf_gspi_dev_t;

static unsigned int alloc_calls;
static unsigned int free_calls;
static unsigned int failed_alloc_calls;
static bool fail_alloc;
static unsigned int io_calls;
static uint16_t observed_len;
static uint8_t observed_function;
static uint32_t observed_address;
static bool observed_write;
static uint32_t *observed_buffer;
static bool fail_read;
static int backend_result;
static uint8_t written_bytes[68];

static void *kmm_malloc(size_t size)
{
  alloc_calls++;
  if (fail_alloc)
    {
      failed_alloc_calls++;
      return NULL;
    }

  return malloc(size);
}

static void kmm_free(void *ptr)
{
  free_calls++;
  free(ptr);
}

static int dma_write(gspi_dev_t *gspi, bool increment, uint8_t function,
                     uint32_t address, uint16_t len, uint32_t *words)
{
  size_t extent = ((size_t)len + 3) & ~(size_t)3;
  (void)gspi;
  assert(increment);
  io_calls++;
  observed_len = len;
  observed_function = function;
  observed_address = address;
  observed_write = true;
  observed_buffer = words;
  assert(extent <= sizeof(written_bytes));
  memcpy(written_bytes, words, extent);
  return backend_result;
}

static int dma_read(gspi_dev_t *gspi, bool increment, uint8_t function,
                    uint32_t address, uint16_t len, uint32_t *words)
{
  size_t extent = ((size_t)len + 3) & ~(size_t)3;
  (void)gspi;
  assert(increment);
  io_calls++;
  observed_len = len;
  observed_function = function;
  observed_address = address;
  observed_write = false;
  observed_buffer = words;
  memset(words, 0x6d, extent);
  return fail_read ? -EIO : backend_result;
}

#include "bcmf-gspi-transfer-function.h"

static gspi_dev_t backend = { dma_write, dma_read };
static bcmf_gspi_dev_t bus = { &backend };

static void verify_call(bool write, unsigned int len)
{
  assert(observed_write == write);
  assert(observed_len == len);
  assert(observed_function == 2);
  assert(observed_address == 0x12345678);
}

static void check_small_byte_transfers(void)
{
  for (unsigned int len = 1; len <= 3; len++)
    {
      uint8_t *heap_buf = malloc(len);
      assert(heap_buf != NULL);
      memset(heap_buf, 0x29, len);

      unsigned int before = io_calls;
      unsigned int old_allocs = alloc_calls;
      assert(bcmf_transfer_bytes(&bus, true, 2, 0x12345678,
                                 heap_buf, len) == 0);
      verify_call(true, len);
      assert(io_calls == before + 1);
      for (unsigned int i = len; i < 4; i++)
        {
          assert(written_bytes[i] == 0);
        }

      memset(heap_buf, 0xa5, len);
      assert(bcmf_transfer_bytes(&bus, false, 2, 0x12345678,
                                 heap_buf, len) == 0);
      verify_call(false, len);
      for (unsigned int i = 0; i < len; i++)
        {
          assert(heap_buf[i] == 0x6d);
        }
      assert(alloc_calls == old_allocs);
      free(heap_buf);
    }
}

static void check_staged_extent(unsigned int len, unsigned int offset)
{
  uint8_t guarded[72];
  uint8_t *buf = guarded + offset;
  unsigned int rounded = (len + 3) & ~3u;
  memset(guarded, 0xc7, sizeof(guarded));
  memset(buf, 0x38, len);

  unsigned int ac = alloc_calls;
  unsigned int fc = free_calls;
  assert(bcmf_transfer_bytes(&bus, true, 2, 0x12345678, buf, len) == 0);
  verify_call(true, len);
  assert(alloc_calls == ac + (len > 4 ? 1u : 0u));
  assert(free_calls == fc + (len > 4 ? 1u : 0u));
  for (unsigned int i = len; i < rounded; i++)
    {
      assert(written_bytes[i] == 0);
    }

  memset(buf, 0xa5, len);
  assert(bcmf_transfer_bytes(&bus, false, 2, 0x12345678, buf, len) == 0);
  verify_call(false, len);
  for (unsigned int i = 0; i < len; i++)
    {
      assert(buf[i] == 0x6d);
    }
  for (unsigned int i = 0; i < offset; i++)
    {
      assert(guarded[i] == 0xc7);
    }
  for (unsigned int i = offset + len; i < sizeof(guarded); i++)
    {
      assert(guarded[i] == 0xc7);
    }
}

static void check_direct_and_errors(void)
{
  uint32_t aligned[16] = {0};
  uint8_t bytes[12];
  unsigned int ac;
  unsigned int before;

  ac = alloc_calls;
  assert(bcmf_transfer_bytes(&bus, true, 2, 0x12345678,
                             (uint8_t *)aligned, sizeof(uint32_t) * 4) == 0);
  verify_call(true, sizeof(uint32_t) * 4);
  assert(alloc_calls == ac);
  assert(observed_buffer == aligned);
  assert(bcmf_transfer_bytes(&bus, false, 2, 0x12345678,
                             (uint8_t *)aligned, sizeof(uint32_t) * 4) == 0);
  verify_call(false, sizeof(uint32_t) * 4);
  assert(alloc_calls == ac);
  assert(observed_buffer == aligned);

  memset(bytes, 0x44, sizeof(bytes));
  fail_read = true;
  before = io_calls;
  assert(bcmf_transfer_bytes(&bus, false, 2, 0x12345678,
                             bytes + 1, 7) == -EIO);
  assert(io_calls == before + 1);
  for (unsigned int i = 0; i < sizeof(bytes); i++)
    {
      assert(bytes[i] == 0x44);
    }
  fail_read = false;

  backend_result = 1;
  assert(bcmf_transfer_bytes(&bus, true, 2, 0x12345678,
                             bytes + 1, 7) == -EIO);
  backend_result = 0;

  fail_alloc = true;
  before = io_calls;
  assert(bcmf_transfer_bytes(&bus, true, 2, 0x12345678,
                             bytes + 1, 7) == -ENOMEM);
  assert(io_calls == before);
  fail_alloc = false;

  before = io_calls;
  assert(bcmf_transfer_bytes(&bus, true, 2, 0, NULL, 1) == -EINVAL);
  assert(bcmf_transfer_bytes(NULL, true, 2, 0, bytes, 1) == -EINVAL);
  assert(bcmf_transfer_bytes(&bus, true, 2, 0, bytes, 0) == 0);
  assert(bcmf_transfer_bytes(&bus, true, 2, 0, bytes, UINT16_MAX + 1u) ==
         -EMSGSIZE);
  assert(io_calls == before);
}

int main(int argc, char **argv)
{
  if (argc == 2 && (strcmp(argv[1], "read") == 0 ||
                    strcmp(argv[1], "write") == 0))
    {
      /* Baseline-only mode: ASan should catch the old direct DMA overread. */
      uint8_t *one_byte = malloc(1);
      assert(one_byte != NULL);
      return bcmf_transfer_bytes(&bus, strcmp(argv[1], "write") == 0,
                                 2, 0x12345678, one_byte, 1);
    }

  check_small_byte_transfers();
  check_staged_extent(5, 0);
  check_staged_extent(7, 1);
  check_staged_extent(63, 2);
  check_staged_extent(5, 3);
  check_staged_extent(4, 1);
  check_staged_extent(64, 1);
  check_direct_and_errors();
  assert(alloc_calls == free_calls + failed_alloc_calls);
  return 0;
}
