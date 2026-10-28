/* Target-header source admission only; no device access or random sampling. */
#include <nuttx/config.h>
#include <stdbool.h>
#include <fcntl.h>

int tls_entropy_open_flags(void)
{
  return O_NONBLOCK;
}

bool tls_rng_admitted(void)
{
#if defined(CONFIG_BUILD_FLAT) && defined(CONFIG_ARCH_CHIP_RP23XX) && \
    defined(CONFIG_RP23XX_RNG) && defined(CONFIG_DEV_RANDOM) && \
    defined(CONFIG_DEV_URANDOM_ARCH) && !defined(CONFIG_SMP) && \
    !defined(CONFIG_DEV_URANDOM_XORSHIFT128) && \
    !defined(CONFIG_DEV_URANDOM_CONGRUENTIAL) && \
    !defined(CONFIG_DEV_URANDOM_RANDOM_POOL) && \
    CONFIG_RP23XX_RNG_POLL_TIMEOUT >= 1000 && \
    CONFIG_RP23XX_RNG_POLL_TIMEOUT <= 1000000
  return true;
#else
  return false;
#endif
}
