/* Register the simulated tracker devices and, when configured, the H4 controller. */
#include <nuttx/config.h>

#include <errno.h>
#include <stdio.h>
#ifdef TRACKER_DEMO_AUTOSTART
#  include <builtin/builtin.h>
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
  if (ret >= 0)
    {
      struct btuart_lowerhalf_s *lower = btuart_shim_getdevice("/dev/ttyS2");
      ret = lower ? btuart_register(lower) : -ENODEV;
    }

#endif

  if (ret < 0)
    {
      printf("tracker simulator initialization failed: %d\n", ret);
      return ret;
    }

#ifdef TRACKER_DEMO_AUTOSTART
  char *tracker_args[] = { "motion_tracker", "--interval-ms", "500", NULL };
  if (exec_builtin(tracker_args[0], tracker_args, NULL) < 0)
    {
      printf("motion tracker startup failed: %d\n", errno);
    }
#endif

  return nsh_main(argc, argv);
}
