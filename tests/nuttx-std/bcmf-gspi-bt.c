/* Execute the patched CYW43439 HCI transport against controller memory.
 * Only kernel/bus boundaries are mocked; firmware parsing and ring operations
 * below come directly from the dependency patch, not a second implementation.
 */
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "bcmf-bt-bus-registers.h"

#define FAR
#define OK 0
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define CONFIG_IOB_BUFSIZE 400
#define CONFIG_IEEE80211_INFINEON_CYW43439_BLUETOOTH 1
#define LP_IFDOWN_TIMEOUT 1
#define SEC2TICK(value) (value)
static char protocol_error[256];
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
static char bus_diagnostic[256];
static char ram_diagnostic[256];
static unsigned ram_diagnostic_fail, ram_reads;
#endif
static void capture_error(const char *format, ...)
{
  va_list arguments;

  if (strncmp(format, "CYW43439 HCI invalid ", 21) != 0)
    {
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
      if (strncmp(format, "BCMF GSPI bus diagnostics ", 26) == 0)
        {
          va_start(arguments, format);
          vsnprintf(bus_diagnostic, sizeof(bus_diagnostic), format, arguments);
          va_end(arguments);
        }
      if (strncmp(format, "CYW43439 HCI RAM diagnostics ",
                  sizeof("CYW43439 HCI RAM diagnostics ") - 1) == 0)
        {
          va_start(arguments, format);
          vsnprintf(ram_diagnostic, sizeof(ram_diagnostic), format, arguments);
          va_end(arguments);
        }
#endif
      return;
    }

  va_start(arguments, format);
  vsnprintf(protocol_error, sizeof(protocol_error), format, arguments);
  va_end(arguments);
}
#define wlerr(...) capture_error(__VA_ARGS__)
typedef pthread_mutex_t mutex_t;
enum bt_buf_type_e { BT_CMD, BT_EVT, BT_ACL_OUT, BT_ACL_IN, BT_ISO_OUT };
struct bt_driver_s {
  size_t head_reserve;
  int (*open)(struct bt_driver_s *);
  int (*send)(struct bt_driver_s *, enum bt_buf_type_e, void *, size_t);
  void (*close)(struct bt_driver_s *);
  int (*receive)(struct bt_driver_s *, enum bt_buf_type_e, void *, size_t);
  void *priv;
};
struct bcmf_bt_s;
enum { gspi_f0_bus = 0 };
struct gspi_dev_s {
  void *priv;
  int (*read)(struct gspi_dev_s *, bool, unsigned, uint32_t, uint16_t, uint32_t *);
};
struct chip_s { uint32_t ram_base, ram_size; };
typedef struct bcmf_gspi_dev_s {
  mutex_t power_lock;
  mutex_t backplane_lock;
  uint32_t backplane_current_addr;
  struct bcmf_bt_s *bt;
  struct gspi_dev_s *gspi;
  struct chip_s *chip;
  bool ready, bt_active;
  unsigned thread_signal;
} bcmf_gspi_dev_t;
struct net_driver_s { bcmf_gspi_dev_t *bus; };
struct bcmf_dev_s {
  bcmf_gspi_dev_t *bus;
  struct net_driver_s bc_dev;
  bool bc_bifup;
  unsigned lp_work_ifdown;
};

static bcmf_gspi_dev_t gbus;
static struct gspi_dev_s gspi;
static struct chip_s chip = {0, 512 * 1024};
static struct bcmf_dev_s priv;
static uint8_t ram[512 * 1024];
static uint8_t patchram[0x1000000];
static uint32_t ctrl, host_ctrl, ram_base, pwrup;
static unsigned writes, deliveries, calls, sleeps, release_after;
static uint32_t fail_addr;
static int receive_result, register_result;
static uint8_t delivered[255];
static size_t delivered_len;
static enum bt_buf_type_e delivered_type;
static bool reopen_during_wait;
static pthread_mutex_t network_lock = PTHREAD_MUTEX_INITIALIZER;
static _Thread_local unsigned network_depth;

static void nxmutex_init(mutex_t *m) { assert(pthread_mutex_init(m, NULL) == 0); }
static int nxmutex_lock(mutex_t *m) { return -pthread_mutex_lock(m); }
static void nxmutex_unlock(mutex_t *m) { assert(pthread_mutex_unlock(m) == 0); }
static void nxmutex_destroy(mutex_t *m) { assert(pthread_mutex_destroy(m) == 0); }
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
static uint32_t diagnostic_window, diagnostic_pattern;
static unsigned diagnostic_reads, diagnostic_fail;
static uint8_t diagnostic_delay;

static int bcmf_read_reg(bcmf_gspi_dev_t *bus, unsigned function,
                         uint32_t address, uint8_t *value) {
  assert(bus == &gbus);
  assert(pthread_mutex_trylock(&bus->backplane_lock) == EBUSY);
  if (++diagnostic_reads == diagnostic_fail) return -EIO;
  if (function == 0) {
    assert(address == CYW_REG_RESP_DELAY_F1);
    *value = diagnostic_delay;
  } else {
    assert(function == 1 && address >= SBSDIO_FUNC1_SBADDRLOW &&
           address <= SBSDIO_FUNC1_SBADDRHIGH);
    *value = diagnostic_window >> (8 * (address - SBSDIO_FUNC1_SBADDRLOW + 1));
  }
  return 0;
}

static int diagnostic_pattern_read(struct gspi_dev_s *bus, bool increment,
                                   unsigned function, uint32_t address,
                                   uint16_t length, uint32_t *value) {
  assert(bus == &gspi && increment && function == gspi_f0_bus);
  assert(address == CYW_REG_TEST_RO && length == 4);
  assert(pthread_mutex_trylock(&gbus.backplane_lock) == EBUSY);
  if (++diagnostic_reads == diagnostic_fail) return EIO;
  *value = diagnostic_pattern;
  return 0;
}
#endif
static void nxsem_post(unsigned *s) { (*s)++; }
static void *kmm_zalloc(size_t n) { return calloc(1, n); }
static void kmm_free(void *p) { free(p); }
static void net_lock(void) {
  assert(pthread_mutex_lock(&network_lock) == 0);
  assert(network_depth++ == 0);
}
static void net_unlock(void) {
  assert(network_depth-- == 1);
  assert(pthread_mutex_unlock(&network_lock) == 0);
}
static int netdev_ifup(struct net_driver_s *d) {
  assert(network_depth == 1);
  d->bus->ready = priv.bc_bifup = true;
  return 0;
}
static int netdev_ifdown(struct net_driver_s *d) {
  /* The native helper does not acquire this lock. Its caller must serialize
   * the lease check and shutdown with HCI open, including the idle worker.
   */
  assert(network_depth == 1);
  if (d->bus->bt_active) return -EBUSY;
  d->bus->ready = priv.bc_bifup = false;
  return 0;
}
static bool bcmf_lowpower_expiration(void *arg, unsigned *work,
                                    void (*worker)(void *), unsigned timeout) {
  (void)work; (void)worker; (void)timeout;
  assert(arg == &priv);
  return true;
}
static int bcmf_gspi_bt_power(bcmf_gspi_dev_t *b, bool on) {
  b->bt_active = on;
  return 0;
}

static uint8_t *memory(uint32_t address, size_t length) {
  if (address == 0x18000c7c && length == 4) return (uint8_t *)&ctrl;
  if (address == 0x18000d6c && length == 4) return (uint8_t *)&host_ctrl;
  if (address == 0x18000d68 && length == 4) return (uint8_t *)&ram_base;
  if (address == 0x19640894 && length == 4) return (uint8_t *)&pwrup;
  if (address >= 0x19000000 && (uint64_t)address + length <= 0x1a000000)
    return patchram + (address - 0x19000000);
  assert((uint64_t)address + length <= sizeof(ram));
  return ram + address;
}
static int transfer(bool write, uint32_t address, uint8_t *buf, size_t length) {
  assert(((uintptr_t)buf | address | length) % 4 == 0);
  assert(length <= 64 && (address & 0xfff) + length <= 0x1000);
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
  if (!write) {
    ram_reads++;
    if ((ram_diagnostic_fail == 1 && length == 4) ||
        (ram_diagnostic_fail == 2 && length == 8)) return -EIO;
  }
#endif
  if (write && address == fail_addr) return -EIO;
  if (write) { writes++; memcpy(memory(address, length), buf, length); }
  else memcpy(buf, memory(address, length), length);
  return 0;
}
static int bcmf_read_sbreg(bcmf_gspi_dev_t *b, uint32_t a, uint8_t *p,
                          unsigned n) {
  assert(b == &gbus);
  return transfer(false, a, p, n);
}
static int bcmf_write_sbreg(bcmf_gspi_dev_t *b, uint32_t a, uint8_t *p,
                           unsigned n) {
  assert(b == &gbus);
  return transfer(true, a, p, n);
}
static int receive_packet(struct bt_driver_s *d, enum bt_buf_type_e type,
                          void *data, size_t len) {
  assert(d != NULL && len <= sizeof(delivered));
  calls++;
  if (receive_result != 0) return receive_result;
  deliveries++;
  memcpy(delivered, data, len);
  delivered_len = len;
  delivered_type = type;
  return 0;
}
static int bt_driver_register(struct bt_driver_s *d) {
  d->receive = receive_packet;
  int result = d->open(d);
  return result < 0 ? result : register_result;
}
static void nxsched_usleep(unsigned usec);

#include "bcmf-bt-transport.h"
#include "bcmf-bt-idle.h"

static void put(uint32_t offset, uint32_t value) {
  uint8_t *p = ram + ram_base + offset;
  p[0] = value; p[1] = value >> 8; p[2] = value >> 16; p[3] = value >> 24;
}
static uint32_t get(uint32_t offset) {
  return bcmf_bt_le32(ram + ram_base + offset);
}
static void nxsched_usleep(unsigned usec) {
  assert(usec == 1000 || usec == 150000);
  sleeps++;
  if (reopen_during_wait) {
    reopen_during_wait = false;
    bcmf_bt_close(&gbus.bt->driver);
    assert(bcmf_bt_open(&gbus.bt->driver) == 0);
  }
  if (release_after != 0 && sleeps >= release_after) {
    /* Backpressure must not hold the HCI lock, otherwise RX cannot progress. */
    assert(pthread_mutex_trylock(&gbus.bt->lock) == 0);
    pthread_mutex_unlock(&gbus.bt->lock);
    put(BCMF_BT_H2B_OUT, get(BCMF_BT_H2B_IN));
    release_after = 0;
  }
}
static const uint8_t valid_fw[] = {
  2, 'v', 0, 255,                  /* version, informational record count */
  2, 0, 0, 4, 0, 1,              /* extended base 0x10000 */
  5, 0xff, 0xfd, 0, 1, 2, 3, 4, 5, /* data crossing a 4 KiB boundary */
  0, 0, 0, 1, 0, 0               /* EOF and optional zero padding */
};
static struct bcmf_bt_s *new_driver(void) {
  assert(gbus.bt == NULL);
  assert(bcmf_gspi_bt_initialize(&gspi, valid_fw, sizeof(valid_fw)) == 0);
  assert(gbus.bt->opened && gbus.bt_active);
  assert(pwrup == 3 && host_ctrl ==
         (BCMF_BT_HOST_WAKE | BCMF_BT_HOST_READY | BCMF_BT_DOORBELL));
  assert(patchram[0x1fffc] == 0xa5 && patchram[0x20002] == 0xa5);
  assert(memcmp(patchram + 0x1fffd, "\1\2\3\4\5", 5) == 0);
  return gbus.bt;
}
static void reset(void) {
  if (gbus.bt != NULL) {
    fail_addr = 0;
    bcmf_bt_close(&gbus.bt->driver);
    nxmutex_destroy(&gbus.bt->lock);
    nxmutex_destroy(&gbus.bt->lifecycle);
    free(gbus.bt);
    gbus.bt = NULL;
  }
  memset(ram, 0, sizeof(ram));
  memset(patchram, 0xa5, sizeof(patchram));
  gbus.ready = gbus.bt_active = false;
  priv.bc_bifup = false;
  ctrl = BCMF_BT_READY | BCMF_BT_AWAKE;
  ram_base = 0x10000;
  host_ctrl = pwrup = 0;
  writes = deliveries = calls = sleeps = release_after = 0;
  fail_addr = 0;
  receive_result = register_result = 0;
  reopen_during_wait = false;
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
  gbus.backplane_current_addr = diagnostic_window = 0x10000;
  diagnostic_pattern = CYW_REG_TEST_RO_PATTERN;
  diagnostic_delay = 4;
  diagnostic_reads = diagnostic_fail = 0;
  bus_diagnostic[0] = '\0';
#endif
}
static void incoming(uint32_t offset, unsigned type, const uint8_t *payload,
                     size_t len) {
  uint32_t words[BCMF_BT_FRAME_WORDS] = {0};
  uint8_t *p = (uint8_t *)words;
  size_t total = (len + 7) & ~3;
  p[0] = len; p[1] = len >> 8; p[2] = len >> 16; p[3] = type;
  assert(len <= BCMF_BT_MAX_PACKET);
  memcpy(p + 4, payload, len);
  for (size_t i = 0; i < total; i++)
    ram[ram_base + BCMF_BT_RING_SIZE + ((offset + i) & BCMF_BT_RING_MASK)] = p[i];
  put(BCMF_BT_B2H_OUT, offset);
  put(BCMF_BT_B2H_IN, (offset + total) & BCMF_BT_RING_MASK);
}

static void firmware_validation(void) {
  struct bcmf_bt_s bt = { .gbus = &gbus, .firmware = valid_fw,
                         .firmware_len = sizeof(valid_fw) };
  for (size_t n = 0; n < sizeof(valid_fw) - 2; n++) {
    bt.firmware_len = n;
    assert(bcmf_bt_firmware(&bt, false) == -EINVAL);
  }
  uint8_t bad[sizeof(valid_fw)];
  memcpy(bad, valid_fw, sizeof(bad));
  bt.firmware = bad; bt.firmware_len = sizeof(bad);
  bad[7] = 3; /* Unsupported record. */
  assert(bcmf_bt_firmware(&bt, false) == -EINVAL);
  assert(bcmf_gspi_bt_initialize(&gspi, bad, sizeof(bad)) == -EINVAL);
  assert(gbus.bt == NULL && writes == 0);
  memcpy(bad, valid_fw, sizeof(bad));
  bad[8] = 0xff; bad[9] = 0xff; /* Extended address outside BT region. */
  assert(bcmf_bt_firmware(&bt, false) == -EINVAL);
  assert(writes == 0);
}

static void transmit(void) {
  struct bcmf_bt_s *bt = new_driver();
  uint8_t command[] = {0x03, 0x0c, 0};
  put(BCMF_BT_H2B_IN, 4092); put(BCMF_BT_H2B_OUT, 4092);
  assert(bcmf_bt_send(&bt->driver, BT_CMD, command, sizeof(command)) == 0);
  assert(get(BCMF_BT_H2B_IN) == 4);
  assert(ram[ram_base + 4092] == 3 && ram[ram_base + 4095] == 1);
  assert(memcmp(ram + ram_base, command, 3) == 0 && ram[ram_base + 3] == 0);
  unsigned before = writes;
  assert(bcmf_bt_send(&bt->driver, BT_ISO_OUT, command, 3) == -EINVAL);
  command[2] = 1;
  assert(bcmf_bt_send(&bt->driver, BT_CMD, command, 3) == -EINVAL);
  assert(writes == before);
  command[2] = 0;
  put(BCMF_BT_H2B_IN, 0); put(BCMF_BT_H2B_OUT, 4); /* Reserved word: full. */
  sleeps = 0; release_after = 2;
  assert(bcmf_bt_send(&bt->driver, BT_CMD, command, 3) == 0);
  assert(sleeps == 2 && get(BCMF_BT_H2B_IN) == 8);
  put(BCMF_BT_H2B_IN, 0); put(BCMF_BT_H2B_OUT, 4);
  before = writes; sleeps = 0;
  assert(bcmf_bt_send(&bt->driver, BT_CMD, command, 3) == -ETIMEDOUT);
  assert(writes == before && sleeps == BCMF_BT_WAIT_COUNT);
}

static void receive_retry_and_faults(void) {
  struct bcmf_bt_s *bt = new_driver();
  const uint8_t event[] = {0x0e, 3, 1, 2, 3};
  incoming(4092, 4, event, sizeof(event));
  receive_result = -ENOMEM;
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && get(BCMF_BT_B2H_OUT) == 4092);
  assert(deliveries == 0 && bt->fault == 0);
  receive_result = 0;
  assert(bcmf_gspi_bt_poll(&gbus) == 1);
  assert(deliveries == 1 && delivered_type == BT_EVT && delivered_len == 5);
  assert(memcmp(delivered, event, sizeof(event)) == 0);
  assert(get(BCMF_BT_B2H_OUT) == 8);
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && deliveries == 1);

  incoming(0, 4, event, sizeof(event));
  put(BCMF_BT_B2H_IN, 4); /* Incomplete frame: no partial delivery. */
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && get(BCMF_BT_B2H_OUT) == 0);
  put(BCMF_BT_B2H_IN, 12);
  fail_addr = ram_base + BCMF_BT_B2H_OUT;
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && bt->fault == -EIO);
  assert(deliveries == 2);
  fail_addr = 0;
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && deliveries == 2); /* No duplicates. */
}

static void malformed_packets(void) {
  const uint8_t bad_event[] = {0x0e, 3, 1};
  uint8_t valid_acl[58] = {0};
  valid_acl[2] = 54;
  /* Addition follows the parenthesized little-endian length expression. */
  assert(bcmf_bt_packet(2, valid_acl, sizeof(valid_acl), true));
  assert(bcmf_bt_packet(2, valid_acl, sizeof(valid_acl), false));
  assert(!bcmf_bt_packet(2, valid_acl, 54, false));
  struct bcmf_bt_s *bt = new_driver();
  incoming(0, 4, bad_event, sizeof(bad_event));
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && bt->fault == -EPROTO);
  assert(calls == 0 && get(BCMF_BT_B2H_OUT) == 0);
  assert(strcmp(protocol_error, "CYW43439 HCI invalid frame type=4 length=3 "
                               "header_length=3 declared=5\n") == 0);
  bt->fault = 0;
  put(BCMF_BT_B2H_IN, 4096);
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
  diagnostic_reads = 0;
#endif
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && bt->fault == -EPROTO);
  assert(strcmp(protocol_error, "CYW43439 HCI invalid indices rx=1 in=4096 out=0\n") == 0);
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
  assert(diagnostic_reads == 5);
  assert(strcmp(bus_diagnostic, "BCMF GSPI bus diagnostics result=0 "
                               "window_match=1 delay=4 pattern_match=1\n") == 0);
  assert(strcmp(ram_diagnostic, "CYW43439 HCI RAM diagnostics base_result=0 "
                               "base_match=1 pair_result=0 pair_match=1 pair_valid=0\n") == 0);
  diagnostic_window ^= 0x8000;
  diagnostic_delay = 8;
  diagnostic_pattern = 0;
#endif
  bt->fault = 0;
  put(BCMF_BT_B2H_IN, 2);
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && bt->fault == -EPROTO);
  assert(strcmp(protocol_error, "CYW43439 HCI invalid indices rx=1 in=2 out=0\n") == 0);
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
  assert(strcmp(bus_diagnostic, "BCMF GSPI bus diagnostics result=0 "
                               "window_match=0 delay=8 pattern_match=0\n") == 0);
  unsigned before_writes = writes;
  diagnostic_reads = 0;
  diagnostic_fail = 2;
  bcmf_gspi_bus_diagnostics(bt->gbus);
  assert(diagnostic_reads == 2 && writes == before_writes && bt->fault == -EPROTO);
  assert(strcmp(bus_diagnostic, "BCMF GSPI bus diagnostics result=-5 "
                               "window_match=0 delay=0 pattern_match=0\n") == 0);
  diagnostic_reads = 0;
  diagnostic_fail = 5;
  bcmf_gspi_bus_diagnostics(bt->gbus);
  assert(diagnostic_reads == 5 && writes == before_writes && bt->fault == -EPROTO);
  assert(strcmp(bus_diagnostic, "BCMF GSPI bus diagnostics result=-5 "
                               "window_match=0 delay=8 pattern_match=0\n") == 0);
  diagnostic_fail = 0;
#endif
  bt->fault = 0;
  put(BCMF_BT_B2H_IN, 4);
  ram[ram_base + 4096] = 0; ram[ram_base + 4097] = 1; /* Length 256. */
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && bt->fault == -EMSGSIZE);
  assert(calls == 0);
}

#ifdef CONFIG_DEBUG_WIRELESS_ERROR
static void ram_fault_diagnostics(void) {
  struct bcmf_bt_s *bt = new_driver();
  uint32_t cached_base = bt->ram_base, in, out;
  unsigned before_reads = ram_reads;
  put(BCMF_BT_B2H_IN, 8); put(BCMF_BT_B2H_OUT, 4);
  ram_diagnostic[0] = 0;
  assert(bcmf_bt_indices(bt, BCMF_BT_B2H_IN, &in, &out) == 0);
  assert(ram_reads == before_reads + 2 && ram_diagnostic[0] == 0);

  put(BCMF_BT_B2H_IN, 0x0200001f); put(BCMF_BT_B2H_OUT, 0x0200001f);
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && bt->fault == -EPROTO);
  unsigned before_writes = writes, before_calls = calls;
  put(BCMF_BT_B2H_IN, 8); put(BCMF_BT_B2H_OUT, 4);
  bcmf_bt_ram_diagnostics(bt, BCMF_BT_B2H_IN, 0x0200001f, 0x0200001f);
  assert(strcmp(ram_diagnostic, "CYW43439 HCI RAM diagnostics base_result=0 "
                               "base_match=1 pair_result=0 pair_match=0 pair_valid=1\n") == 0);
  ram_base += 4;
  bcmf_bt_ram_diagnostics(bt, BCMF_BT_B2H_IN, 0x0200001f, 0x0200001f);
  assert(strcmp(ram_diagnostic, "CYW43439 HCI RAM diagnostics base_result=0 "
                               "base_match=0 pair_result=0 pair_match=0 pair_valid=1\n") == 0);
  ram_base = cached_base;
  ram_diagnostic_fail = 1;
  bcmf_bt_ram_diagnostics(bt, BCMF_BT_B2H_IN, 0x0200001f, 0x0200001f);
  assert(strcmp(ram_diagnostic, "CYW43439 HCI RAM diagnostics base_result=-5 "
                               "base_match=0 pair_result=0 pair_match=0 pair_valid=1\n") == 0);
  ram_diagnostic_fail = 2;
  bcmf_bt_ram_diagnostics(bt, BCMF_BT_B2H_IN, 0x0200001f, 0x0200001f);
  assert(strcmp(ram_diagnostic, "CYW43439 HCI RAM diagnostics base_result=0 "
                               "base_match=1 pair_result=-5 pair_match=0 pair_valid=0\n") == 0);
  ram_diagnostic_fail = 0;
  before_reads = ram_reads;
  assert(bcmf_gspi_bt_poll(&gbus) == 0 && bt->fault == -EPROTO);
  assert(ram_reads == before_reads && writes == before_writes && calls == before_calls);
  assert(bt->ram_base == cached_base);
}
#endif

static void *producer(void *argument) {
  struct bcmf_bt_s *bt = gbus.bt;
  unsigned id = (uintptr_t)argument;
  for (unsigned i = 0; i < 64; i++) {
    uint8_t command[] = {3, 12, 4, id, i, 0, 0};
    assert(bcmf_bt_send(&bt->driver, BT_CMD, command, sizeof(command)) == 0);
  }
  return NULL;
}
static void concurrent_transmit(void) {
  new_driver();
  pthread_t producers[2];
  for (uintptr_t i = 0; i < 2; i++)
    assert(pthread_create(&producers[i], NULL, producer, (void *)i) == 0);
  for (unsigned i = 0; i < 2; i++) assert(pthread_join(producers[i], NULL) == 0);
  assert(get(BCMF_BT_H2B_IN) == 128 * 12);
  bool seen[2][64] = {{false}};
  for (size_t offset = 0; offset < 128 * 12; offset += 12) {
    uint8_t *p = ram + ram_base + offset;
    assert(p[0] == 7 && p[3] == 1);
    assert(p[7] < 2 && p[8] < 64 && !seen[p[7]][p[8]]);
    seen[p[7]][p[8]] = true;
  }
}

static void boot_errors_and_close(void) {
  ctrl = 0;
  assert(bcmf_gspi_bt_initialize(&gspi, valid_fw, sizeof(valid_fw)) == -ETIMEDOUT);
  assert(!gbus.bt->opened && !gbus.bt_active && sleeps == 301);
  reset();
  ram_base = sizeof(ram) - BCMF_BT_B2H_OUT;
  assert(bcmf_gspi_bt_initialize(&gspi, valid_fw, sizeof(valid_fw)) == -EPROTO);
  assert(!gbus.bt->opened && !gbus.bt_active);
  reset();
  register_result = -EIO;
  assert(bcmf_gspi_bt_initialize(&gspi, valid_fw, sizeof(valid_fw)) == -EIO);
  assert(!gbus.bt->opened && !gbus.bt_active);
  reset();
  struct bcmf_bt_s *bt = new_driver();
  unsigned before = writes;
  assert(bcmf_bt_open(&bt->driver) == -EALREADY && writes == before);
  assert(bcmf_gspi_bt_initialize(&gspi, valid_fw, sizeof(valid_fw)) == -EALREADY);
  bcmf_lowpower_ifdown_work(&priv);
  assert(gbus.ready && priv.bc_bifup && gbus.bt_active);
  bcmf_bt_close(&bt->driver);
  assert(!bt->opened && !gbus.bt_active && host_ctrl == BCMF_BT_DOORBELL);
  bcmf_lowpower_ifdown_work(&priv);
  assert(!gbus.ready && !priv.bc_bifup);
  uint8_t command[] = {3, 12, 0};
  assert(bcmf_bt_send(&bt->driver, BT_CMD, command, 3) == -ENODEV);
  assert(bcmf_gspi_bt_poll(&gbus) == 0);
}

static void close_failures_and_stale_send(void) {
  struct bcmf_bt_s *bt = new_driver();
  fail_addr = BCMF_BT_HOST_CTRL;
  bcmf_bt_close(&bt->driver);
  assert(!bt->opened && bt->fault == -EIO && gbus.bt_active);
  fail_addr = 0;
  bcmf_bt_close(&bt->driver);
  assert(!gbus.bt_active);
  assert(bcmf_bt_open(&bt->driver) == 0);
  put(BCMF_BT_H2B_IN, 0); put(BCMF_BT_H2B_OUT, 4); /* Full ring. */
  reopen_during_wait = true;
  uint8_t command[] = {3, 12, 0};
  assert(bcmf_bt_send(&bt->driver, BT_CMD, command, 3) == -ENODEV);
  assert(bt->opened && bt->fault == 0 && get(BCMF_BT_H2B_IN) == 0);
}

int main(void) {
  nxmutex_init(&gbus.power_lock);
  nxmutex_init(&gbus.backplane_lock);
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
  gspi.read = diagnostic_pattern_read;
#endif
  gspi.priv = &priv; priv.bus = &gbus; priv.bc_dev.bus = &gbus;
  gbus.gspi = &gspi; gbus.chip = &chip;
  reset(); firmware_validation();
  reset(); transmit();
  reset(); receive_retry_and_faults();
  reset(); malformed_packets();
#ifdef CONFIG_DEBUG_WIRELESS_ERROR
  reset(); ram_fault_diagnostics();
#endif
  reset(); concurrent_transmit();
  reset(); boot_errors_and_close();
  reset(); close_failures_and_stale_send();
  reset();
  nxmutex_destroy(&gbus.power_lock);
  nxmutex_destroy(&gbus.backplane_lock);
  puts("CYW43439: firmware, wraparound, backpressure, malformed RX, retry, concurrent TX, lifecycle: PASS");
  return 0;
}
