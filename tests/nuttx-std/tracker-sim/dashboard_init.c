/* Register the simulated tracker devices and, when configured, the H4 controller. */
#include <nuttx/config.h>

#include <errno.h>
#include <stdio.h>

#ifdef CONFIG_RAM_UART
#include <stddef.h>
#include <nuttx/serial/uart_ram.h>

struct uart_rambuf_s tracker_hci_buffers[2];
_Static_assert(offsetof(struct uart_rambuf_s, wroff) == CONFIG_RAM_UART_BUFSIZE &&
               offsetof(struct uart_rambuf_s, rdoff) == CONFIG_RAM_UART_BUFSIZE + 4 &&
               sizeof(struct uart_rambuf_s) == CONFIG_RAM_UART_BUFSIZE + 8,
               "RAM UART layout must match the qtest peer");
#endif
#ifdef CONFIG_BLUETOOTH_UART_SHIM
#include <nuttx/wireless/bluetooth/bt_uart.h>
#include <nuttx/wireless/bluetooth/bt_uart_shim.h>
#endif

int tracker_sim_devices(void);
int nsh_main(int argc, char *argv[]);

int tracker_sim_init(int argc, char *argv[])
{
  int ret = tracker_sim_devices();
#ifdef CONFIG_BLUETOOTH_UART_SHIM
#ifdef CONFIG_RAM_UART
  const char *hci = "/dev/tracker_hci";
  if (ret >= 0)
    {
      ret = uart_ram_register(hci, tracker_hci_buffers, false);
    }
#else
  const char *hci = "/dev/ttyS2";
#endif
  if (ret >= 0)
    {
      struct btuart_lowerhalf_s *lower = btuart_shim_getdevice(hci);
      ret = lower ? btuart_register(lower) : -ENODEV;
    }

#endif

  if (ret < 0)
    {
      printf("tracker simulator initialization failed: %d\n", ret);
      return ret;
    }

  return nsh_main(argc, argv);
}
