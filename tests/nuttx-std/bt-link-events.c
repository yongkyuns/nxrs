/* Verify actual event trace blocks and native packed HCI layouts. */
#include <assert.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define begin_packed_struct
#define end_packed_struct __attribute__((packed))
#define LOG_INFO 6
#define TICK_PER_SEC 100
#define BT_LE162HOST(value) (value)
typedef struct { uint8_t type; uint8_t val[6]; } bt_addr_le_t;
#include "bt-link-event-structs.h"

static unsigned long ticks;
static unsigned int calls;
static char message[256];

unsigned long clock_systime_ticks(void) { return ticks; }

void syslog(int priority, const char *format, ...) __attribute__((format(printf, 2, 3)));
void syslog(int priority, const char *format, ...)
{
  va_list args;
  int length;
  assert(priority == LOG_INFO);
  va_start(args, format);
  length = vsnprintf(message, sizeof(message), format, args);
  va_end(args);
  assert(length > 0 && (size_t)length < sizeof(message));
  calls++;
}

static void connected(struct bt_hci_evt_le_conn_complete_s *evt)
{
  (void)evt;
#include "bt-link-event-connect.h"
}

static void disconnected(struct bt_hci_evt_disconn_complete_s *evt)
{
  (void)evt;
#include "bt-link-event-disconnect.h"
}

int main(void)
{
  struct bt_hci_evt_le_conn_complete_s connection;
  struct bt_hci_evt_disconn_complete_s disconnection;
  _Static_assert(sizeof(connection) == 18, "HCI LE connection event layout");
  _Static_assert(offsetof(struct bt_hci_evt_le_conn_complete_s, interval) == 11,
                 "HCI LE interval offset");
  memset(&connection, 0xa5, sizeof(connection));
  memset(&disconnection, 0xa5, sizeof(disconnection));
  connection.status = 0;
  connection.role = 1;
  connection.interval = 24;
  connection.latency = 0;
  connection.supv_timeout = 400;
  disconnection.status = 0;
  disconnection.reason = 8;
  ticks = 1000;
  connected(&connection);
#ifdef CONFIG_BLUETOOTH_LINK_TRACE
  assert(calls == 1);
  assert(strcmp(message, "BT_LINK_EVENT connected=1 status=0 role=1 interval=24 "
                 "latency=0 timeout=400 ticks=1000 tick_hz=100\n") == 0);
  ticks = 1400;
  disconnected(&disconnection);
  assert(calls == 2);
  assert(strcmp(message, "BT_LINK_EVENT connected=0 status=0 reason=8 "
                 "ticks=1400 tick_hz=100\n") == 0);
  for (unsigned int status = 1; status <= 255; status++)
    {
      char expected[256];
      connection.status = status;
      connected(&connection);
      snprintf(expected, sizeof(expected), "BT_LINK_EVENT connected=1 status=%u "
                "role=0 interval=0 latency=0 timeout=0 ticks=1400 tick_hz=100\n", status);
      assert(strcmp(message, expected) == 0);
      disconnection.status = status;
      disconnected(&disconnection);
      snprintf(expected, sizeof(expected), "BT_LINK_EVENT connected=0 status=%u "
                "reason=0 ticks=1400 tick_hz=100\n", status);
      assert(strcmp(message, expected) == 0);
    }
#else
  disconnected(&disconnection);
  assert(calls == 0);
#endif
  return 0;
}
