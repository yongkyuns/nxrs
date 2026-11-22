/* Test image startup: connect the existing H4 lower half before starting NSH. */
#include <errno.h>
#include <stdio.h>
#include <nuttx/wireless/bluetooth/bt_uart.h>
#include <nuttx/wireless/bluetooth/bt_uart_shim.h>

int tracker_sim_devices(void);
int nsh_main(int argc, char *argv[]);

int tracker_sim_init(int argc, char *argv[])
{
  int ret = tracker_sim_devices();
  if (ret >= 0)
    {
      struct btuart_lowerhalf_s *lower = btuart_shim_getdevice("/dev/ttyS2");
      ret = lower ? btuart_register(lower) : -ENODEV;
    }

  if (ret < 0)
    {
      printf("tracker simulator initialization failed: %d\n", ret);
      return ret;
    }

  return nsh_main(argc, argv);
}
