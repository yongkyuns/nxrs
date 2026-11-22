/* Start a separate Rust archive task before exposing the tracker in NSH. */
#include <nuttx/config.h>
#include <builtin/builtin.h>
#include <errno.h>
#include <stdio.h>
#include <sys/mount.h>
#include <syslog.h>

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

  if (mount(NULL, "/logs", "tmpfs", 0, NULL) < 0)
    {
      ret = -errno;
      printf("log filesystem mount failed: %d\n", ret);
      return ret;
    }

  syslog(LOG_NOTICE, "log witness: before tracker and archive task\n");
  char *args[] =
    {
      "motion_tracker", "--drain-system-log", CONFIG_SYSLOG_DEVPATH,
      "/logs/system.log", "512", NULL
    };
  if (exec_builtin(args[0], args, NULL) < 0)
    {
      ret = -errno;
      printf("log archive task startup failed: %d\n", ret);
      return ret;
    }

  return nsh_main(argc, argv);
}
