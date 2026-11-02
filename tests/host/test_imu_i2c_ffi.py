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

    def test_sensor_readiness_cancellation_and_gpio_controls(self):
        """Real host poll/pipe witness; fake GPIO headers, not target/ISR qualification."""
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "nuttx/ioexpander").mkdir(parents=True)
            (work / "nuttx/config.h").write_text("")
            (work / "nuttx/ioexpander/gpio.h").write_text("""
 enum gpio_pintype_e { GPIO_OUTPUT_PIN, GPIO_INTERRUPT_RISING_PIN };
 #define GPIOC_READ 1001
 #define GPIOC_PINTYPE 1002
 #define GPIOC_REGISTER 1003
 #define GPIOC_UNREGISTER 1004
""")
            # macOS lacks pipe2; the shim uses real nonblocking host pipes.
            (work / "compat.h").write_text("int sensor_test_pipe2(int fds[2], int flags);\n")
            harness = work / "events.c"
            harness.write_text(r"""
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <nuttx/ioexpander/gpio.h>
int gnss_event_pipe(int[2]);
int imu_event_pipe(int[2]);
int gnss_event_wait(int, int);
int imu_event_wait(int, int);
int imu_irq_check(int);
int imu_irq_register(int);
int imu_irq_unregister(int);
int imu_irq_read(int);
static int type = GPIO_INTERRUPT_RISING_PIN, registered;
int sensor_test_pipe2(int fds[2], int flags) {
  assert((flags & O_NONBLOCK) && (flags & O_CLOEXEC));
  assert(pipe(fds) == 0);
  for (int i = 0; i < 2; i++) {
    assert(fcntl(fds[i], F_SETFL, O_NONBLOCK) == 0);
    assert(fcntl(fds[i], F_SETFD, FD_CLOEXEC) == 0);
  }
  return 0;
}
int ioctl(int fd, unsigned long request, ...) {
  assert(fd == 42);
  if (request == GPIOC_REGISTER) { registered++; return 0; }
  if (request == GPIOC_UNREGISTER) { registered--; return 0; }
  va_list args; va_start(args, request);
  void *target = (void *)(uintptr_t)va_arg(args, unsigned long); va_end(args);
  if (request == GPIOC_PINTYPE) *(enum gpio_pintype_e *)target = type;
  else { assert(request == GPIOC_READ); *(bool *)target = true; }
  return 0;
}
struct waiting { int device, cancel; int (*wait)(int, int); };
static void *waiter(void *opaque) {
  struct waiting *w = opaque;
  assert(w->wait(w->device, w->cancel) == 0);
  return NULL;
}
static void check(int (*create)(int[2]), int (*wait)(int,int)) {
  int device[2], cancel[2]; char value;
  assert(create(device) == 0 && create(cancel) == 0);
  assert(write(device[1], "d", 1) == 1);
  assert(wait(device[0], cancel[0]) == 1);
  assert(read(device[0], &value, 1) == 1);
  pthread_t thread;
  struct waiting w = {device[0], cancel[0], wait};
  assert(pthread_create(&thread, NULL, waiter, &w) == 0);
  assert(write(cancel[1], "s", 1) == 1);
  assert(pthread_join(thread, NULL) == 0);
  assert(write(device[1], "d", 1) == 1);
  assert(wait(device[0], cancel[0]) == 0); /* cancellation wins */
  for (int i = 0; i < 2; i++) { close(device[i]); close(cancel[i]); }
}
int main(void) {
  check(gnss_event_pipe, gnss_event_wait);
  check(imu_event_pipe, imu_event_wait);
  assert(imu_irq_check(42) == 0);
  type = GPIO_OUTPUT_PIN;
  assert(imu_irq_check(42) == -1 && errno == EINVAL);
  assert(imu_irq_register(42) == 0 && registered == 1);
  assert(imu_irq_read(42) == 1);
  assert(imu_irq_unregister(42) == 0 && registered == 0);
}
""")
            executable = work / "events"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                            "-D_GNU_SOURCE", "-Dpipe2=sensor_test_pipe2", "-include", str(work / "compat.h"),
                            "-I", str(work), str(harness),
                            str(ROOT / "hal/gnss/src/platform/nuttx/ffi/events.c"),
                            str(ROOT / "hal/imu/src/platform/nuttx/ffi/events.c"),
                            "-o", str(executable)], check=True, timeout=30)
            subprocess.run([str(executable)], check=True, timeout=5)
