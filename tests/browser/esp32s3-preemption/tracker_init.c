/* Start the dashboard fixture on ESP-EMU without its external H4 controller. */

#include <stdio.h>
#include <stdbool.h>

bool esp32s3_qemu_entropy_available(void);
int tracker_sim_devices(void);
int nsh_main(int argc, char *argv[]);

int tracker_sim_init(int argc, char *argv[])
{
  int ret = tracker_sim_devices();
  if (ret < 0)
    {
      printf("tracker simulator initialization failed: %d\n", ret);
      return ret;
    }

  printf("tracker simulator host entropy: %s\n",
         esp32s3_qemu_entropy_available() ? "available" : "unavailable");
  return nsh_main(argc, argv);
}
