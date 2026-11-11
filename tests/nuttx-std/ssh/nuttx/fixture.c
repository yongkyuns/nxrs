/* SPDX-License-Identifier: MIT
 * Qualification-only target ABI translation. Rust owns commands, deadlines,
 * child ownership, secret input framing, provisioning and cleanup policy. */
#include <nuttx/config.h>
#include <builtin/builtin.h>
#include <fsutils/passwd.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

/* One interrupt witness at a time in the qualification session. No default
 * signal actions or asynchronous resource cleanup are enabled for Rust. */
static volatile sig_atomic_t g_interrupted;
static void ssh_probe_sigint(int signal_number)
{
  (void)signal_number;
  g_interrupted = 1;
}

void *nxrs_ssh_test_interrupt_begin(void)
{
  struct sigaction *saved = calloc(1, sizeof(*saved));
  struct sigaction action = {0};
  if (saved == NULL) return NULL;
  g_interrupted = 0;
  action.sa_handler = ssh_probe_sigint;
  sigemptyset(&action.sa_mask);
  if (sigaction(SIGINT, &action, saved) < 0) { free(saved); return NULL; }
  return saved;
}

int nxrs_ssh_test_interrupt_seen(void) { return g_interrupted != 0; }

int nxrs_ssh_test_interrupt_end(void *state)
{
  int result = sigaction(SIGINT, state, NULL) < 0 ? errno : 0;
  free(state);
  return result;
}

int nxrs_ssh_test_spawn(const char *action)
{
  char *argv[] = {"dropbear", (char *)action, NULL};
  int pid = exec_builtin("dropbear", argv, NULL);
  return pid < 0 ? -errno : pid;
}

int nxrs_ssh_test_wait(int pid, int *result)
{
  int status = 0;
  int found = waitpid(pid, &status, WNOHANG);
  if (found < 0) return -errno;
  if (found == 0) return 0;
  *result = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
  return 1;
}

int nxrs_ssh_test_readable(int timeout_ms)
{
  struct pollfd fd = { .fd = STDIN_FILENO, .events = POLLIN };
  int result = poll(&fd, 1, timeout_ms);
  if (result < 0) return errno == EINTR ? 0 : -errno;
  if (fd.revents & (POLLERR | POLLHUP | POLLNVAL)) return -EIO;
  return (fd.revents & POLLIN) != 0;
}

int nxrs_ssh_test_winsize(uint16_t *rows, uint16_t *columns)
{
  struct winsize size = {0};
  if (ioctl(STDIN_FILENO, TIOCGWINSZ, (unsigned long)&size) < 0) return errno;
  *rows = size.ws_row;
  *columns = size.ws_col;
  return 0;
}

void *nxrs_ssh_test_raw_begin(void)
{
  struct termios *saved = calloc(1, sizeof(*saved));
  struct termios raw;
  if (saved == NULL) return NULL;
  if (tcgetattr(STDIN_FILENO, saved) < 0) { free(saved); return NULL; }
  raw = *saved;
  raw.c_lflag &= ~(ECHO | ECHONL | ICANON | ISIG);
  raw.c_iflag &= ~(ICRNL | INLCR | IGNCR | IXON | IXOFF | ISTRIP);
  raw.c_cc[VMIN] = 1;
  raw.c_cc[VTIME] = 0;
  if (tcsetattr(STDIN_FILENO, TCSANOW, &raw) < 0) { free(saved); return NULL; }
  return saved;
}

int nxrs_ssh_test_raw_end(void *state, int discard)
{
  int error = 0;
  if (discard && tcflush(STDIN_FILENO, TCIFLUSH) < 0) error = errno;
  if (tcsetattr(STDIN_FILENO, TCSANOW, state) < 0) error = errno;
  free(state);
  return error;
}

const char *nxrs_ssh_test_key_path(void)
{
#ifdef CONFIG_NETUTILS_DROPBEAR
  return CONFIG_NETUTILS_DROPBEAR_HOSTKEY_PATH;
#else
  return NULL;
#endif
}
const char *nxrs_ssh_test_passwd_path(void)
{
#ifdef CONFIG_FSUTILS_PASSWD
  return CONFIG_FSUTILS_PASSWD_PATH;
#else
  return NULL;
#endif
}
int nxrs_ssh_test_adduser(const char *user, const char *password)
{
#if defined(CONFIG_FSUTILS_PASSWD) && !defined(CONFIG_FSUTILS_PASSWD_READONLY)
  return passwd_adduser(user, password);
#else
  (void)user;
  (void)password;
  return -ENOSYS;
#endif
}
