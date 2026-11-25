/* NuttX SPI sequence ABI only; Rust validates and assembles the transaction. */
#include <nuttx/config.h>
#include <nuttx/spi/spi_transfer.h>
#include <sys/ioctl.h>
#include <stdint.h>
#include <stddef.h>
#include <errno.h>
int spi_resource_transfer(int fd, uint16_t select, uint32_t frequency,
                          uint8_t mode, const uint8_t *tx, uint8_t *rx, size_t len)
{
#if defined(CONFIG_SPI_EXCHANGE) && defined(CONFIG_SPI_DRIVER)
  static const uint8_t modes[] = { SPIDEV_MODE0, SPIDEV_MODE1, SPIDEV_MODE2, SPIDEV_MODE3 };
  if (mode > 3 || !frequency || !len || len > 256) { errno = EINVAL; return -1; }
  struct spi_trans_s trans = { .deselect = true, .nwords = len,
                              .txbuffer = tx, .rxbuffer = rx };
  struct spi_sequence_s seq = { .dev = SPIDEV_USER(select), .mode = modes[mode],
    .nbits = 8, .ntrans = 1, .frequency = frequency, .trans = &trans };
  return ioctl(fd, SPIIOC_TRANSFER, (unsigned long)(uintptr_t)&seq);
#else
  (void)fd; (void)select; (void)frequency; (void)mode; (void)tx; (void)rx; (void)len;
  errno = ENOTSUP;
  return -1;
#endif
}
