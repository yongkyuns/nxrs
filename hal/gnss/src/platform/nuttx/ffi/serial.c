/* Target termios layout and individual SDK controls only. */
#include <nuttx/config.h>
#include <stdint.h>
#include <fcntl.h>
#include <string.h>
#include <termios.h>
_Static_assert(sizeof(struct termios) <= 64, "termios snapshot bound");
int gnss_serial_nonblocking_flag(void) { return O_NONBLOCK; }
int gnss_serial_capture(int fd, uint8_t saved[64])
{
  struct termios value;
  memset(&value, 0, sizeof(value));
  if (tcgetattr(fd, &value) < 0) return -1;
  memcpy(saved, &value, sizeof(value));
  return 0;
}
int gnss_serial_configure(int fd, const uint8_t saved[64], uint32_t baud)
{
  struct termios value;
  memcpy(&value, saved, sizeof(value));
  cfmakeraw(&value);
  value.c_cflag = (value.c_cflag & ~(CSIZE | CSTOPB | PARENB)) | CS8 | CREAD | CLOCAL;
#ifdef CRTSCTS
  value.c_cflag &= ~CRTSCTS;
#endif
  value.c_cc[VMIN] = 1;
  value.c_cc[VTIME] = 0;
  if (cfsetispeed(&value, baud) < 0 || cfsetospeed(&value, baud) < 0) return -1;
  return tcsetattr(fd, TCSANOW, &value);
}
int gnss_serial_restore(int fd, const uint8_t saved[64])
{
  struct termios value;
  memcpy(&value, saved, sizeof(value));
  return tcsetattr(fd, TCSANOW, &value);
}
