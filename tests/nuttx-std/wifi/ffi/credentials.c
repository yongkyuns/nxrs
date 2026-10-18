/* SPDX-License-Identifier: MIT */
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

/* Fixture-only input: lengths + raw SSID + password. Never use NSH arguments or
 * history for a password. Set raw/no-echo BEFORE publishing the ready marker. */
int nxrs_wifi_test_credentials(uint8_t ssid[32], uint8_t password[63],
                               uint32_t *ssid_length, uint32_t *password_length)
{
  /* NuttX software-only TTYs do not fill the hardware termios fields. */
  struct termios saved = {0};
  struct termios input;
  struct timespec now;
  uint8_t frame[2 + 32 + 63] = {0};
  size_t received = 0;
  size_t expected = 2;
  int result = 0;
  time_t deadline;
  if (tcgetattr(STDIN_FILENO, &saved) < 0) return errno;
  input = saved;
  input.c_lflag &= ~(ECHO | ECHONL | ICANON | ISIG);
  input.c_iflag &= ~(ICRNL | INLCR | IGNCR | IXON | IXOFF | ISTRIP);
  input.c_cc[VMIN] = 1;
  input.c_cc[VTIME] = 0;
  if (tcsetattr(STDIN_FILENO, TCSANOW, &input) < 0) return errno;
  clock_gettime(CLOCK_MONOTONIC, &now);
  deadline = now.tv_sec + 60;
  puts("NXRS_WIFI_CREDENTIALS_READY");
  fflush(stdout);
  while (received < expected)
    {
      struct pollfd fd = { .fd = STDIN_FILENO, .events = POLLIN };
      ssize_t count;
      clock_gettime(CLOCK_MONOTONIC, &now);
      if (now.tv_sec >= deadline) { result = ETIMEDOUT; break; }
      if (poll(&fd, 1, 1000) < 0)
        {
          if (errno == EINTR) continue;
          result = errno; break;
        }
      if ((fd.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) { result = EIO; break; }
      if ((fd.revents & POLLIN) == 0) continue;
      count = read(STDIN_FILENO, frame + received, expected - received);
      if (count <= 0) { result = EIO; break; }
      received += count;
      if (received == 2)
        {
          if (frame[0] == 0 || frame[0] > 32 || frame[1] > 63 ||
              (frame[1] != 0 && frame[1] < 8)) { result = EINVAL; break; }
          expected = 2 + frame[0] + frame[1];
        }
    }
  if (result == 0)
    {
      *ssid_length = frame[0];
      *password_length = frame[1];
      memcpy(ssid, frame + 2, frame[0]);
      memcpy(password, frame + 2 + frame[0], frame[1]);
    }
  explicit_bzero(frame, sizeof(frame));
  /* Do not return to an echoing shell with secret bytes still in its input. */
  if (result != 0) tcflush(STDIN_FILENO, TCIFLUSH);
  if (tcsetattr(STDIN_FILENO, TCSANOW, &saved) < 0) return errno;
  return result;
}
