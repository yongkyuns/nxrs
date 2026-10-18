/* Compile the actual trace helpers/transport wrapper, with no radio access. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#define FAR
#define LOG_INFO 6
#define TICK_PER_SEC 100
#define BT_HCI_EVT_LE_META_EVENT 0x3e
#define BT_HCI_EVT_LE_CONN_COMPLETE 1
#define BT_HCI_EVT_DISCONN_COMPLETE 5
enum bt_buf_type_e { BT_CMD, BT_EVT, BT_ACL_IN, BT_ACL_OUT };
typedef unsigned int irqstate_t;
static unsigned int critical;
static unsigned long ticks;
static unsigned int calls;
static unsigned int sends;
static int send_result;
static char messages[32][160];
#ifdef CONFIG_BLUETOOTH_LINK_TRACE
static bool restart_on_log;
static void log_reentry(void);
#endif

struct bt_driver_s;
struct bt_buf_s { enum bt_buf_type_e type; uint8_t *data; unsigned int len; };
struct bt_driver_s
{
  int (*send)(struct bt_driver_s *, enum bt_buf_type_e, void *, size_t);
};

irqstate_t enter_critical_section(void) { return critical++; }
void leave_critical_section(irqstate_t old)
{
  assert(critical == old + 1);
  critical = old;
}
unsigned long clock_systime_ticks(void) { return ticks; }
void syslog(int priority, const char *format, ...)
  __attribute__((format(printf, 2, 3)));
void syslog(int priority, const char *format, ...)
{
  va_list args;
  int count;
  assert(critical == 0 && priority == LOG_INFO && calls < 32);
  va_start(args, format);
  count = vsnprintf(messages[calls], sizeof(messages[calls]), format, args);
  va_end(args);
  assert(count > 0 && (size_t)count < sizeof(messages[calls]));
  calls++;
#ifdef CONFIG_BLUETOOTH_LINK_TRACE
  if (restart_on_log)
    {
      restart_on_log = false;
      log_reentry();
    }
#endif
}

#include "bt-setup-stage-functions.h"

#ifdef CONFIG_BLUETOOTH_LINK_TRACE
static void log_reentry(void)
{
  ticks = 3000;
  bt_hci_setup_trace(BT_SETUP_BEGIN, 0);
}
#endif

static int driver_send(struct bt_driver_s *dev, enum bt_buf_type_e type,
                       void *data, size_t len)
{
  (void)dev;
  (void)type;
  (void)data;
  (void)len;
  assert(critical == 0);
  sends++;
  return send_result;
}

int main(void)
{
  struct bt_driver_s driver = { .send = driver_send };
  uint8_t bytes[21] = { 0x3e, 19, 1, 0 };
  struct bt_buf_s packet = { .type = BT_ACL_OUT, .data = bytes, .len = 12 };
#ifdef CONFIG_BLUETOOTH_LINK_TRACE
  /* Short/malformed events cannot read out of bounds or arm a window. */
  bt_hci_setup_trace(BT_SETUP_ATT_RX, 2);
  for (size_t length = 0; length < sizeof(bytes); length++)
    {
      uint8_t *short_event = malloc(length ? length : 1);
      memcpy(short_event, bytes, length);
      bt_setup_received(BT_EVT, short_event, length);
      free(short_event);
    }
  bytes[3] = 1;
  bt_setup_received(BT_EVT, bytes, sizeof(bytes));
  bytes[3] = 0;
  bytes[1] = 18;
  bt_setup_received(BT_EVT, bytes, sizeof(bytes));
  assert(calls == 0);
  bytes[1] = 19;
  ticks = 100;
  bt_setup_received(BT_EVT, bytes, sizeof(bytes));
  assert(calls == 0);
  bt_hci_setup_trace((enum bt_setup_stage_e)-1, 0);
  bt_hci_setup_trace((enum bt_setup_stage_e)11, 0);
  for (unsigned int repeat = 0; repeat < 1000; repeat++)
    {
      ticks = 101 + repeat;
      bt_setup_received(BT_ACL_IN, NULL, 12);
      bt_hci_setup_trace(BT_SETUP_ACL_DISPATCH, 12);
      bt_hci_setup_trace(BT_SETUP_L2CAP_RX, 4);
      bt_hci_setup_trace(BT_SETUP_ATT_RX, 2);
      bt_hci_setup_trace(BT_SETUP_ATT_TX, 3);
      assert(bt_send(&driver, &packet) == 0);
      bt_hci_setup_trace(BT_SETUP_READY, 0);
    }
  assert(calls == 0 && sends == 1000);
  uint8_t disconnect[] = { 5, 4, 0, 0xa5, 0xa5, 8 };
  ticks = 1142;
  bt_setup_received(BT_EVT, disconnect, sizeof(disconnect));
  assert(calls == 0);
  ticks = 1143;
  bt_hci_setup_trace(BT_SETUP_END, 8);
  assert(calls == 11);
  assert(strcmp(messages[0], "BT_SETUP_EVENT window=1 stage=0 value=0 "
                 "ticks=100 tick_hz=100\n") == 0);
  for (unsigned int stage = 1; stage <= 8; stage++)
    {
      char expected[160];
      int values[] = { 0, 12, 12, 4, 2, 3, 12, 0, 0 };
      snprintf(expected, sizeof(expected),
               "BT_SETUP_EVENT window=1 stage=%u value=%d "
               "ticks=101 tick_hz=100\n", stage, values[stage]);
      assert(strcmp(messages[stage], expected) == 0);
    }
  assert(strcmp(messages[9], "BT_SETUP_EVENT window=1 stage=9 value=8 "
                 "ticks=1142 tick_hz=100\n") == 0);
  assert(strcmp(messages[10], "BT_SETUP_EVENT window=1 stage=10 value=8 "
                 "ticks=1143 tick_hz=100\n") == 0);
  bt_hci_setup_trace(BT_SETUP_ATT_RX, 2);
  bt_hci_setup_trace(BT_SETUP_END, 8);
  assert(calls == 11);

  /* A transport error is returned unchanged and recorded in a new window. */
  ticks = 2000;
  bt_setup_received(BT_EVT, bytes, sizeof(bytes));
  ticks = 2001;
  send_result = -5;
  assert(bt_send(&driver, &packet) == -5);
  assert(calls == 11 && sends == 1001);
  packet.type = BT_CMD;
  assert(bt_send(&driver, &packet) == -5 && sends == 1002);
  assert(calls == 11 && critical == 0);

  /* Output can start a new window without overwriting the private snapshot. */
  ticks = 2002;
  restart_on_log = true;
  bt_hci_setup_trace(BT_SETUP_END, 19);
  assert(calls == 15 && g_setup_window == 3 && g_setup_active);
  assert(strstr(messages[11], "window=2 stage=0 value=0 ticks=2000 ") != NULL);
  assert(strstr(messages[12], "window=2 stage=6 value=12 ticks=2001 ") != NULL);
  assert(strstr(messages[13], "window=2 stage=7 value=-5 ticks=2001 ") != NULL);
  assert(strstr(messages[14], "window=2 stage=10 value=19 ticks=2002 ") != NULL);
  ticks = 3001;
  bt_hci_setup_trace(BT_SETUP_ATT_RX, 2);
  assert(calls == 15);
  ticks = 3002;
  bt_hci_setup_trace(BT_SETUP_END, 19);
  assert(calls == 18);
  assert(strstr(messages[15], "window=3 stage=0 value=0 ticks=3000 ") != NULL);
  assert(strstr(messages[16], "window=3 stage=4 value=2 ticks=3001 ") != NULL);
  assert(strstr(messages[17], "window=3 stage=10 value=19 ticks=3002 ") != NULL);

  /* The documented global model replaces an unfinished overlapping window. */
  bt_hci_setup_trace(BT_SETUP_BEGIN, 0);
  bt_hci_setup_trace(BT_SETUP_ATT_RX, 2);
  bt_hci_setup_trace(BT_SETUP_BEGIN, 0);
  assert(calls == 18);
  bt_hci_setup_trace(BT_SETUP_END, 19);
  assert(calls == 20);
  assert(strstr(messages[18], "window=5 stage=0 ") != NULL);
  assert(strstr(messages[19], "window=5 stage=10 ") != NULL);
  bt_hci_setup_trace(BT_SETUP_END, 19);
  assert(calls == 20 && critical == 0);
#else
  unsigned int side_effects = 0;
  bt_hci_setup_trace(++side_effects, ++side_effects);
  assert(side_effects == 0);
  assert(bt_send(&driver, &packet) == 0 && sends == 1);
  send_result = -5;
  assert(bt_send(&driver, &packet) == -5 && sends == 2);
  assert(calls == 0);
#endif
  return 0;
}
