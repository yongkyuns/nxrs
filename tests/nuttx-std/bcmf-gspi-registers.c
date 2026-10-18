/* Native regression witness for patched bcmf_gspi register and power paths. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define OK 0
#define wlinfo(...) ((void)0)
#define wlerr(...) ((void)0)

#include "bcmf-gspi-register-constants.h"

typedef struct gspi_dev_s gspi_dev_t;
struct gspi_dev_s
{
  int (*write)(gspi_dev_t *, bool, enum gspi_cmd_func_e, uint32_t,
               uint16_t, uint32_t *);
  int (*read)(gspi_dev_t *, bool, enum gspi_cmd_func_e, uint32_t,
              uint16_t, uint32_t *);
};

typedef struct
{
  gspi_dev_t *gspi;
  bool ready;
  bool sleeping;
  bool kso_enable;
} bcmf_gspi_dev_t;

static unsigned int io_calls;
static unsigned int sleep_calls;
static unsigned int fail_call;
static int fail_result;
static uint32_t read_value;
static uint16_t observed_len;
static uint32_t observed_address;
static uint32_t observed_write_value;
static bool observed_write;
static enum gspi_cmd_func_e observed_function;

static int mock_io(bool write, enum gspi_cmd_func_e function,
                   uint32_t address, uint16_t len, uint32_t *words)
{
  size_t extent = ((size_t)len + 3) & ~(size_t)3;
  io_calls++;
  observed_write = write;
  observed_function = function;
  observed_address = address;
  observed_len = len;
  if (write)
    {
      memcpy(&observed_write_value, words, sizeof(observed_write_value));
    }
  else
    {
      assert(extent <= sizeof(read_value));
      memcpy(words, &read_value, extent);
    }

  return io_calls == fail_call ? fail_result : OK;
}

static int mock_write(gspi_dev_t *gspi, bool increment,
                      enum gspi_cmd_func_e function, uint32_t address,
                      uint16_t len, uint32_t *words)
{
  (void)gspi;
  assert(increment);
  return mock_io(true, function, address, len, words);
}

static int mock_read(gspi_dev_t *gspi, bool increment,
                     enum gspi_cmd_func_e function, uint32_t address,
                     uint16_t len, uint32_t *words)
{
  (void)gspi;
  assert(increment);
  return mock_io(false, function, address, len, words);
}

static void nxsched_usleep(unsigned int usec)
{
  (void)usec;
  sleep_calls++;
}

#include "bcmf-gspi-register-functions.h"

static gspi_dev_t device = { mock_write, mock_read };
static bcmf_gspi_dev_t bus = { &device, true, false, false };

static void reset_mock(void)
{
  io_calls = 0;
  sleep_calls = 0;
  fail_call = 0;
  fail_result = -EIO;
  read_value = 0;
  observed_len = 0;
  observed_address = 0;
  observed_write_value = 0;
  observed_write = false;
  observed_function = gspi_f0_bus;
}

static void check_register_widths_and_errors(void)
{
  uint32_t value32 = 0xaaaaaaaa;
  uint16_t value16 = 0xaaaa;
  uint8_t value8 = 0xaa;

  reset_mock();
  read_value = 0x12345678;
  assert(bcmf_gspi_read_reg_32(&device, gspi_f0_bus, 0x1234,
                                &value32) == OK);
  assert(value32 == read_value && observed_len == 4);
  read_value = 0x12345678;
  assert(bcmf_gspi_read_reg_16(&device, gspi_f0_bus, 0x1234,
                                &value16) == OK);
  assert(value16 == 0x5678 && observed_len == 2);
  assert(bcmf_gspi_read_reg_8(&device, gspi_f1_backplane, 0x1234,
                               &value8) == OK);
  assert(value8 == 0x78 && observed_len == 1);
  assert(bcmf_gspi_write_reg_16(&device, gspi_f0_bus, 0x4321,
                                 0x1234) == OK);
  assert(observed_write && observed_len == 2 &&
         observed_write_value == 0x1234);
  assert(bcmf_gspi_write_reg_8(&device, gspi_f1_backplane, 0x4321,
                                0x5a) == OK);
  assert(observed_write && observed_len == 1 && observed_write_value == 0x5a);

  value32 = 0xaaaaaaaa;
  fail_call = io_calls + 1;
  fail_result = -ENODEV;
  read_value = 0x87654321;
  assert(bcmf_gspi_read_reg_32(&device, gspi_f0_bus, 0, &value32) == -ENODEV);
  assert(value32 == 0xaaaaaaaa);
  fail_call = io_calls + 1;
  fail_result = 1;
  value16 = 0xaaaa;
  assert(bcmf_gspi_read_reg_16(&device, gspi_f0_bus, 0, &value16) == -EIO);
  assert(value16 == 0xaaaa);
  fail_call = io_calls + 1;
  fail_result = -EPIPE;
  value8 = 0xaa;
  assert(bcmf_gspi_read_reg_8(&device, gspi_f0_bus, 0, &value8) == -EPIPE);
  assert(value8 == 0xaa);
  fail_call = io_calls + 1;
  fail_result = 1;
  assert(bcmf_gspi_write_reg_16(&device, gspi_f0_bus, 0, 1) == -EIO);
  fail_call = io_calls + 1;
  fail_result = -EIO;
  assert(bcmf_gspi_write_reg_8(&device, gspi_f0_bus, 0, 1) == -EIO);
  assert(bcmf_gspi_read_reg_8(NULL, gspi_f0_bus, 0, &value8) == -EINVAL);
  assert(bcmf_gspi_read_reg_8(&device, gspi_f0_bus, 0, NULL) == -EINVAL);
  assert(bcmf_gspi_write_reg_8(NULL, gspi_f0_bus, 0, 0) == -EINVAL);
}

static void check_public_register_api(void)
{
  uint8_t value = 0xa5;

  reset_mock();
  read_value = 0x42;
  assert(bcmf_read_reg(&bus, gspi_f1_backplane, SBSDIO_FUNC1_SLEEPCSR,
                       &value) == OK);
  assert(value == 0x42 && observed_len == 1);
  fail_call = io_calls + 1;
  assert(bcmf_read_reg(&bus, 1, 0, &value) == -EIO && value == 0x42);
  fail_call = io_calls + 1;
  fail_result = 1;
  assert(bcmf_write_reg(&bus, 1, 0, 3) == -EIO);
  assert(bcmf_read_reg(NULL, 1, 0, &value) == -EINVAL);
  assert(bcmf_write_reg(NULL, 1, 0, 0) == -EINVAL);
}

static void check_kso_state_and_force(void)
{
  reset_mock();
  read_value = SBSDIO_FUNC1_SLEEPCSR_KSO_MASK;
  assert(bcmf_gspi_kso_enable(&bus, true, false) == -ETIMEDOUT);
  assert(!bus.kso_enable && io_calls == 398);

  reset_mock();
  read_value = SBSDIO_FUNC1_SLEEPCSR_DEVON_MASK;
  assert(bcmf_gspi_kso_enable(&bus, true, false) == -ETIMEDOUT);
  assert(!bus.kso_enable);

  reset_mock();
  read_value = SBSDIO_FUNC1_SLEEPCSR_KSO_MASK |
               SBSDIO_FUNC1_SLEEPCSR_DEVON_MASK;
  assert(bcmf_gspi_kso_enable(&bus, true, false) == OK);
  assert(bus.kso_enable && io_calls == 2);

  reset_mock();
  bus.kso_enable = false;
  fail_call = 1;
  assert(bcmf_gspi_kso_enable(&bus, true, false) == -EIO);
  assert(!bus.kso_enable);
  reset_mock();
  fail_call = 2;
  assert(bcmf_gspi_kso_enable(&bus, true, false) == -EIO);
  assert(!bus.kso_enable);
  reset_mock();
  bus.kso_enable = true;
  assert(bcmf_gspi_kso_enable(&bus, true, false) == OK && io_calls == 0);
  read_value = SBSDIO_FUNC1_SLEEPCSR_KSO_MASK |
               SBSDIO_FUNC1_SLEEPCSR_DEVON_MASK;
  assert(bcmf_gspi_kso_enable(&bus, true, true) == OK && io_calls == 2);
}

static void check_sleep_state_and_force(void)
{
  reset_mock();
  fail_call = 1;
  assert(bcmf_gspi_bus_sleep(&bus, true, false) == -EIO);
  assert(!bus.sleeping);

  reset_mock();
  bus.sleeping = true;
  fail_call = 1;
  assert(bcmf_gspi_bus_sleep(&bus, false, false) == -EIO);
  assert(bus.sleeping);
  reset_mock();
  bus.sleeping = true;
  fail_call = 2;
  assert(bcmf_gspi_bus_sleep(&bus, false, false) == -EIO);
  assert(bus.sleeping);
  reset_mock();
  bus.sleeping = false;
  assert(bcmf_gspi_bus_sleep(&bus, false, false) == OK && io_calls == 0);
  read_value = SBSDIO_HT_AVAIL;
  assert(bcmf_gspi_bus_sleep(&bus, false, true) == OK && io_calls == 2);
  assert(!bus.sleeping);
}

static void check_init_error_propagation(void)
{
  reset_mock();
  assert(bcmf_gspi_setup_interrupts(&bus) == OK && io_calls == 2);
  reset_mock();
  fail_call = 1;
  assert(bcmf_gspi_setup_interrupts(&bus) == -EIO && io_calls == 1);
  reset_mock();
  fail_call = 2;
  assert(bcmf_gspi_setup_interrupts(&bus) == -EIO && io_calls == 2);

  reset_mock();
  read_value = SBSDIO_ALP_AVAIL;
  assert(bcmf_gspi_init_alp_clock(&bus) == OK && io_calls == 3);
  reset_mock();
  fail_call = 1;
  assert(bcmf_gspi_init_alp_clock(&bus) == -EIO && io_calls == 1);
  reset_mock();
  fail_call = 2;
  assert(bcmf_gspi_init_alp_clock(&bus) == -EIO && io_calls == 2);
  reset_mock();
  fail_call = 3;
  read_value = SBSDIO_ALP_AVAIL;
  assert(bcmf_gspi_init_alp_clock(&bus) == -EIO && io_calls == 3);

  reset_mock();
  read_value = REV16(CYW_REG_TEST_RO_PATTERN);
  fail_call = 2;
  assert(bcmf_gspi_init_device(&bus) == -EIO && io_calls == 2);
  reset_mock();
  read_value = REV16(CYW_REG_TEST_RO_PATTERN);
  assert(bcmf_gspi_init_device(&bus) == OK && io_calls == 3);
}

int main(void)
{
  check_register_widths_and_errors();
  check_public_register_api();
  bus.ready = true;
  check_kso_state_and_force();
  check_sleep_state_and_force();
  check_init_error_propagation();
  (void)sleep_calls;
  return 0;
}
