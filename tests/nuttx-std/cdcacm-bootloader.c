/* Exercise the actual patched USB request branch, with kernel/ROM boundaries
 * mocked. No copy of the production trigger logic is kept in this fixture.
 */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define USB_DIR_OUT 0
#define USB_REQ_TYPE_CLASS 0x20
#define USB_REQ_RECIPIENT_INTERFACE 1
#define ACM_SET_LINE_CODING 0x20
#define SIZEOF_CDC_LINECODING 7
#define CDCACM_EVENT_LINECODING 0
#define GETUINT32(p) ((uint32_t)(p)[0] | (uint32_t)(p)[1] << 8 | \
                     (uint32_t)(p)[2] << 16 | (uint32_t)(p)[3] << 24)

typedef unsigned long wdparm_t;
typedef unsigned int irqstate_t;
struct wdog_s { void (*func)(wdparm_t); wdparm_t arg; };
struct cdc_linecoding_s { uint8_t baud[4], stop, parity, nbits; };
struct cdcacm_dev_s
{
  struct cdc_linecoding_s linecoding;
  struct wdog_s bootloader;
  struct { unsigned int ifnobase; } devinfo;
  void (*callback)(int);
};
struct usb_ctrlreq_s { unsigned int type; };
static unsigned int callbacks;
static void notification(int event)
{
  assert(event == CDCACM_EVENT_LINECODING);
  ++callbacks;
}
#define usbtrace(...) ((void)0)

#ifdef CONFIG_CDCACM_1200BPS_BOOTLOADER
#define WDOG_ISACTIVE(w) ((w)->func != NULL)
#define MSEC2TICK(ms) (((ms) + 9) / 10)
#define ROM_FUNC_REBOOT 0x5242
#define REBOOT2_FLAG_REBOOT_TYPE_BOOTSEL 2
#define REBOOT2_FLAG_NO_RETURN_ON_SUCCESS 0x100
typedef void (*rom_reboot_fn)(uint32_t, uint32_t, uint32_t, uint32_t);
static unsigned int queued, rebooted, critical;
static void rom_reboot(uint32_t flags, uint32_t delay,
                       uint32_t arg0, uint32_t arg1)
{
  assert(flags == (REBOOT2_FLAG_REBOOT_TYPE_BOOTSEL |
                   REBOOT2_FLAG_NO_RETURN_ON_SUCCESS));
  assert(delay == 10 && arg0 == 0 && arg1 == 0);
  ++rebooted;
}
static rom_reboot_fn rom_func_lookup(unsigned int function)
{
  assert(function == ROM_FUNC_REBOOT);
  return rom_reboot;
}
static irqstate_t enter_critical_section(void) { return critical++; }
static void leave_critical_section(irqstate_t flags)
{
  assert(--critical == flags);
}
static int wd_start(struct wdog_s *timer, unsigned int ticks,
                    void (*callback)(wdparm_t), wdparm_t arg)
{
  assert(critical && ticks == MSEC2TICK(100));
  assert(!timer->func);
  timer->func = callback;
  timer->arg = arg;
  ++queued;
  return 0;
}
#include "cdcacm-rom.h"
#include "cdcacm-reset.h"
#endif

static int request(struct cdcacm_dev_s *priv, unsigned int type,
                   unsigned int index, unsigned int len,
                   uint8_t *dataout, size_t outlen)
{
  struct usb_ctrlreq_s control = { .type = type };
  const struct usb_ctrlreq_s *ctrl = &control;
  int ret = -EOPNOTSUPP;
  switch (ACM_SET_LINE_CODING)
    {
#include "cdcacm-linecoding.h"
    }
  return ret;
}

int main(void)
{
  struct cdcacm_dev_s device = { .devinfo.ifnobase = 2,
                               .callback = notification };
  uint8_t storage[8] = { 0, 0xb0, 4, 0, 0, 0, 0, 8 };
  uint8_t *magic = storage + 1; /* Deliberately unaligned 1200 baud. */
  uint8_t normal[7] = { 0, 0xc2, 1, 0, 0, 0, 8 }; /* 115200 baud. */
  assert(request(&device, 0x21, 2, 7, normal, 7) == 0);
  assert(GETUINT32(device.linecoding.baud) == 115200);
  unsigned int before = callbacks;
  assert(request(&device, 0x21, 2, 7, magic, 6) == -EINVAL);
  assert(request(&device, 0x21, 2, 7, magic, 8) == -EINVAL);
  assert(request(&device, 0x21, 2, 6, magic, 7) == -EOPNOTSUPP);
  assert(request(&device, 0x21, 3, 7, magic, 7) == -EOPNOTSUPP);
  assert(request(&device, 0xa1, 2, 7, magic, 7) == -EOPNOTSUPP);
  assert(callbacks == before);
  assert(GETUINT32(device.linecoding.baud) == 115200);
  assert(request(&device, 0x21, 2, 7, NULL, 0) == 0);
#ifdef CONFIG_CDCACM_1200BPS_BOOTLOADER
  assert(queued == 0 && rebooted == 0);
#endif
  assert(request(&device, 0x21, 2, 7, magic, 7) == 0);
  assert(GETUINT32(device.linecoding.baud) == 1200);
  assert(request(&device, 0x21, 2, 7, NULL, 0) == 0);
  assert(request(&device, 0x21, 2, 7, magic, 7) == 0);
  assert(request(&device, 0x21, 2, 7, normal, 7) == 0);
#ifdef CONFIG_CDCACM_1200BPS_BOOTLOADER
  /* Neither repeated requests nor a later baud change postpone/cancel reset. */
  assert(queued == 1 && rebooted == 0 && critical == 0);
  assert(device.bootloader.func);
  device.bootloader.func(device.bootloader.arg);
  assert(rebooted == 1);
#else
  assert(!device.bootloader.func);
#endif
  return 0;
}
