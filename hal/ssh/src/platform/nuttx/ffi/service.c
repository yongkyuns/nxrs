/* SPDX-License-Identifier: MIT */
#include <nuttx/config.h>
#include <builtin/builtin.h>
#include <errno.h>
#include <stddef.h>
#include <sys/wait.h>

int maintenance_spawn(void)
{
  char *argv[] = {"dropbear", "start", NULL};
  int pid = exec_builtin("dropbear", argv, NULL);
  return pid < 0 ? -errno : pid;
}

int maintenance_reaped(int pid)
{
  int found = waitpid(pid, NULL, WNOHANG);
  return found < 0 ? (errno == EINTR ? 0 : -errno) : found;
}

const char *maintenance_hostkey(void)
{
  return CONFIG_NETUTILS_DROPBEAR_HOSTKEY_PATH;
}

const char *maintenance_passwd(void)
{
  return CONFIG_FSUTILS_PASSWD_PATH;
}

const char *maintenance_bind_address(void)
{
  return CONFIG_NETUTILS_DROPBEAR_BIND_ADDRESS;
}
