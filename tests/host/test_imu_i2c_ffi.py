"""Message-semantics regression for the real bridge using fake host headers.

The CoreS3 cross-build separately checks actual NuttX headers/ABI. This fixture
checks repeated START, STOP, bounds and error propagation; not electrical I/O.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ImuI2cBridgeTests(unittest.TestCase):
    def test_atomic_register_read_and_write_only_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            headers = {
                "nuttx/config.h": "",
                "sys/ioctl.h": "int ioctl(int, int, ...);\n",
                "nuttx/i2c/i2c_master.h": """
 #include <stdint.h>
 #define I2C_M_READ 1
 #define I2C_M_NOSTOP 64
 #define I2CIOC_TRANSFER 123
 struct i2c_msg_s { uint32_t frequency; uint16_t addr, flags;
                     uint8_t *buffer; int length; };
 struct i2c_transfer_s { struct i2c_msg_s *msgv; int msgc; };
""",
            }
            for name, source in headers.items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
            harness = work / "check.c"
            harness.write_text(r"""
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <nuttx/i2c/i2c_master.h>
int imu_i2c_transfer(int, uint8_t, uint32_t, const uint8_t *, size_t, uint8_t *, size_t);
static int mode, calls;
int ioctl(int fd, int request, ...) {
  assert(fd == 42 && request == I2CIOC_TRANSFER); calls++;
  va_list args; va_start(args, request);
  struct i2c_transfer_s *t = (void *)(uintptr_t)va_arg(args, unsigned long);
  va_end(args);
  assert(t->msgc == (mode == 0 ? 2 : 1));
  for (int i = 0; i < t->msgc; i++) {
    assert(t->msgv[i].frequency == 400000 && t->msgv[i].addr == 0x68);
    assert(t->msgv[i].length == 1);
  }
  if (mode == 0) {
    assert(t->msgv[0].flags == I2C_M_NOSTOP);
    assert(t->msgv[0].buffer[0] == 0x21);
    assert(t->msgv[1].flags == I2C_M_READ);
    t->msgv[1].buffer[0] = 1;
  } else {
    assert(t->msgv[0].flags == (mode == 2 ? I2C_M_READ : 0));
  }
  if (mode == 3) { errno = ETIMEDOUT; return -1; }
  return 0;
}
int main(void) {
  uint8_t reg = 0x21, data = 0;
  assert(imu_i2c_transfer(42, 0x68, 400000, &reg, 1, &data, 1) == 0 && data == 1);
  mode = 1; assert(imu_i2c_transfer(42, 0x68, 400000, &reg, 1, 0, 0) == 0);
  mode = 2; assert(imu_i2c_transfer(42, 0x68, 400000, 0, 0, &data, 1) == 0);
  mode = 3; assert(imu_i2c_transfer(42, 0x68, 400000, &reg, 1, 0, 0) == -1);
  assert(errno == ETIMEDOUT && calls == 4);
  assert(imu_i2c_transfer(42, 0x68, 400000, &reg, 65, 0, 0) == -1 && errno == EINVAL);
  assert(imu_i2c_transfer(42, 0x68, 400000, 0, 0, 0, 0) == 0 && calls == 4);
}
""")
            executable = work / "check"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(work), str(harness),
                            str(ROOT / "hal/imu/src/platform/nuttx/ffi/i2c.c"),
                            "-o", str(executable)], check=True, timeout=30)
            subprocess.run([str(executable)], check=True, timeout=5)
