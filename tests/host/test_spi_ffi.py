"""SPI sequence ABI witness with fake headers; not electrical qualification."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SpiBridgeTests(unittest.TestCase):
    def test_one_locked_sequence_and_disabled_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "nuttx/spi").mkdir(parents=True)
            (work / "sys").mkdir()
            (work / "sys/ioctl.h").write_text("int ioctl(int, int, ...);\n")
            (work / "nuttx/spi/spi_transfer.h").write_text(r"""
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#ifdef CONFIG_SPI_EXCHANGE
/* Deliberately non-identity enum values prove native mode translation. */
#define SPIDEV_MODE0 10
#define SPIDEV_MODE1 11
#define SPIDEV_MODE2 12
#define SPIDEV_MODE3 13
#define SPIDEV_USER(n) (0x80000u | (n))
#define SPIIOC_TRANSFER 456
struct spi_trans_s { bool deselect; bool cmd; uint32_t hwfeat, delay;
  size_t nwords; const void *txbuffer; void *rxbuffer; };
struct spi_sequence_s { uint32_t dev; uint8_t mode, nbits, ntrans;
  uint32_t frequency, a, b, c, i; struct spi_trans_s *trans; };
#endif
""")
            harness = work / "check.c"
            harness.write_text(r"""
#include <nuttx/config.h>
#include <nuttx/spi/spi_transfer.h>
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
int spi_resource_transfer(int, uint16_t, uint32_t, uint8_t, const uint8_t *, uint8_t *, size_t);
#ifdef CONFIG_SPI_EXCHANGE
static int calls, fail;
int ioctl(int fd, int request, ...) {
  assert(fd == 42 && request == SPIIOC_TRANSFER); calls++;
  va_list args; va_start(args, request);
  struct spi_sequence_s *s = (void *)(uintptr_t)va_arg(args, unsigned long);
  va_end(args);
  assert(s->dev == SPIDEV_USER(7) && s->mode == SPIDEV_MODE3);
  assert(s->nbits == 8 && s->frequency == 4000000 && s->ntrans == 1);
  assert(s->a == 0 && s->b == 0 && s->c == 0 && s->i == 0);
  struct spi_trans_s *t = s->trans;
  assert(t->deselect && t->nwords == 3 && t->delay == 0 && t->hwfeat == 0 && !t->cmd);
  assert(((const uint8_t *)t->txbuffer)[0] == 0x80);
  ((uint8_t *)t->rxbuffer)[2] = 0x24;
  if (fail) { errno = ETIMEDOUT; return -1; }
  return 0;
}
#endif
int main(void) {
  uint8_t tx[3] = {0x80}, rx[3] = {0};
#ifdef CONFIG_SPI_EXCHANGE
  assert(spi_resource_transfer(42, 7, 4000000, 3, tx, rx, 3) == 0 && rx[2] == 0x24);
  fail = 1;
  assert(spi_resource_transfer(42, 7, 4000000, 3, tx, rx, 3) == -1 && errno == ETIMEDOUT);
  assert(spi_resource_transfer(42, 7, 4000000, 4, tx, rx, 3) == -1 && errno == EINVAL);
  assert(spi_resource_transfer(42, 7, 4000000, 3, tx, rx, 257) == -1 && errno == EINVAL);
  assert(calls == 2);
#else
  assert(spi_resource_transfer(42, 7, 4000000, 3, tx, rx, 3) == -1 && errno == ENOTSUP);
#endif
}
""")
            for enabled in (False, True):
                (work / "nuttx/config.h").write_text(
                    "#define CONFIG_SPI_EXCHANGE 1\n#define CONFIG_SPI_DRIVER 1\n" if enabled else "")
                binary = work / "check"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(work),
                                str(harness), str(ROOT / "driver/spi/src/platform/nuttx/ffi/spi.c"),
                                "-o", str(binary)], check=True, timeout=30)
                subprocess.run([str(binary)], check=True, timeout=5)
