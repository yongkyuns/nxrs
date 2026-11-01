/* Synchronous SDK operation; Rust admits the explicit initialization policy. */
#include <nuttx/config.h>
#include <errno.h>
#if !defined(CONFIG_NETINIT_THREAD) && defined(CONFIG_NETUTILS_NETINIT)
#include <netutils/netinit.h>
#endif
int wifi_initialize_network(void)
{
#if !defined(CONFIG_NETINIT_THREAD) && defined(CONFIG_NETUTILS_NETINIT)
  return netinit_bringup();
#else
  return -ENOTSUP;
#endif
}
