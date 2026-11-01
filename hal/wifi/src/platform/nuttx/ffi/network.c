/* Direct-init profiles defer network bring-up until Wi-Fi acquisition. */
#include <nuttx/config.h>
#if defined(CONFIG_INIT_ENTRY) && !defined(CONFIG_NETINIT_THREAD) && defined(CONFIG_NETUTILS_NETINIT)
#include <netutils/netinit.h>
#endif
#include <errno.h>
int wifi_initialize_network(void)
{
#ifdef CONFIG_INIT_ENTRY
#  if defined(CONFIG_NETINIT_THREAD) || !defined(CONFIG_NETUTILS_NETINIT)
  return -ENOTSUP;
#  else
  return netinit_bringup();
#  endif
#else
  /* The shell's normal initialization owns network bring-up. */
  return 0;
#endif
}
