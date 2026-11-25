/* Register the shared tracker devices before exposing the simulated console. */
#include <stdio.h>

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

  return nsh_main(argc, argv);
}
