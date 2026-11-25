/* Translate a write-only display transaction to the NuttX SPI ABI. */

#include <nuttx/config.h>
#include <nuttx/spi/spi_transfer.h>
#include <sys/ioctl.h>

#if !defined(CONFIG_SPI_DRIVER) || !defined(CONFIG_SPI_CMDDATA)
#  error "The display requires NuttX command/data SPI sequences"
#endif

int display_spi_write(int fd, uint32_t frequency, uint8_t command,
                      const uint8_t *data, size_t len)
{
  struct spi_trans_s transfer[] =
    {
      { .cmd = true, .nwords = 1, .txbuffer = &command },
      { .nwords = len, .txbuffer = data },
    };
  struct spi_sequence_s sequence =
    {
      .dev = SPIDEV_DISPLAY(0),
      .mode = SPIDEV_MODE0,
      .nbits = 8,
      .ntrans = len == 0 ? 1 : 2,
      .frequency = frequency,
      .trans = transfer,
    };

  return ioctl(fd, SPIIOC_TRANSFER, (unsigned long)&sequence);
}
