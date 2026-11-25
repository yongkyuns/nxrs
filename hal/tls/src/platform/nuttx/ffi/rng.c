/* Target-header source admission. Rust owns access and random sampling. */
#include <nuttx/config.h>
#include <stdbool.h>
#include <fcntl.h>

#if defined(CONFIG_BUILD_FLAT) && defined(CONFIG_ARCH_CHIP_ESP32S3) && \
    defined(CONFIG_ESP32S3_QEMU_IMAGE) && defined(CONFIG_ESP32S3_RNG) && \
    defined(CONFIG_DEV_RANDOM)
extern bool esp32s3_qemu_entropy_available(void);
#endif

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
#elif defined(CONFIG_BUILD_FLAT) && defined(CONFIG_ARCH_CHIP_ESP32S3) && \
      defined(CONFIG_ESP32S3_RNG) && defined(CONFIG_DEV_RANDOM) && \
      (defined(CONFIG_ESPRESSIF_WIFI) || defined(CONFIG_ESPRESSIF_BLE)) && \
      !defined(CONFIG_PM) && !defined(CONFIG_ESPRESSIF_AUTO_SLEEP) && \
      !defined(CONFIG_ESP32S3_AUTO_SLEEP)
  return true;
#elif defined(CONFIG_BUILD_FLAT) && defined(CONFIG_ARCH_CHIP_ESP32S3) && \
      defined(CONFIG_ESP32S3_QEMU_IMAGE) && defined(CONFIG_ESP32S3_RNG) && \
      defined(CONFIG_DEV_RANDOM)
  return esp32s3_qemu_entropy_available();
#else
  return false;
#endif
}
