/* Target poll/pipe ABI only. Rust owns waits, cancellation and device policy. */
#include <nuttx/config.h>
#include <poll.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
int acquisition_event_pipe(int descriptors[2])
{
  return pipe2(descriptors, O_NONBLOCK | O_CLOEXEC);
}
int acquisition_event_wait(int device, int cancel)
{
  struct pollfd fds[2] = {
    { .fd = device, .events = POLLIN },
    { .fd = cancel, .events = POLLIN }
  };
  int result = poll(fds, 2, -1);
  if (result < 0) return -1;
  if (fds[1].revents & POLLIN) return 0;
  if ((fds[0].revents | fds[1].revents) & (POLLERR | POLLHUP | POLLNVAL))
    { errno = EIO; return -1; }
  if (fds[0].revents & POLLIN) return 1;
  errno = EINTR;
  return -1;
}

/* One descriptor and a caller-supplied timeout; Rust owns the cadence. */
int acquisition_event_tick(int cancel, int timeout_ms)
{
  struct pollfd fd = { .fd = cancel, .events = POLLIN };
  int result = poll(&fd, 1, timeout_ms);
  if (result < 0) return -1;
  if (fd.revents & POLLIN) return 0;
  if (fd.revents & (POLLERR | POLLHUP | POLLNVAL))
    { errno = EIO; return -1; }
  if (result == 0) return 1;
  errno = EINTR;
  return -1;
}
