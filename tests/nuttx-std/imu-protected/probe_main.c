/* Protected userspace witness. Link the production IMU ABI bridges unchanged. */
#include <nuttx/config.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

#if !defined(CONFIG_BUILD_PROTECTED) || defined(__KERNEL__)
#  error "This witness requires a protected userspace build"
#endif

int imu_witness_i2c(void);
int acquisition_event_pipe(int descriptors[2]);
int acquisition_event_wait(int device, int cancel);
int acquisition_event_tick(int cancel, int timeout_ms);
int gpio_resource_irq_check(int fd);
int gpio_resource_irq_register(int fd);
int gpio_resource_irq_unregister(int fd);
int gpio_resource_irq_read(int fd);

struct wake_request
{
  pthread_t reader;
  int cancel;
  int status;
};

static volatile sig_atomic_t signaled;

static void mark_signal(int signal_number)
{
  (void)signal_number;
  signaled = 1;
}

static void *wake_reader(void *arg)
{
  struct wake_request *request = arg;
  struct timespec wait = { .tv_nsec = 100000000 };

  while (nanosleep(&wait, &wait) < 0 && errno == EINTR)
    {
    }

  if (request->cancel >= 0)
    {
      request->status = write(request->cancel, "c", 1) == 1 ? 0 : -1;
    }
  else
    {
      request->status = pthread_kill(request->reader, SIGUSR1);
    }

  return NULL;
}

static int check_wait(int gpio, int cancel, int writer)
{
  struct wake_request request = { pthread_self(), writer, -1 };
  pthread_t worker;
  if (pthread_create(&worker, NULL, wake_reader, &request) != 0)
    {
      return -1;
    }

  int result = acquisition_event_wait(gpio, cancel);
  int error = errno;
  if (pthread_join(worker, NULL) != 0 || request.status != 0)
    {
      return -1;
    }

  return (writer >= 0 ? result == 0 : result == -1 && error == EINTR
                                               && signaled) ? 0 : -1;
}

static int check_gpio(void)
{
  int gpio = open("/dev/gpio-imu-witness", O_RDONLY);
  int cancel[2];
  int registered = 0;
  int result = -1;
  char byte;
  struct sigaction action = { .sa_handler = mark_signal };
  struct sigaction previous;
  if (gpio < 0)
    {
      return -1;
    }

  if (acquisition_event_pipe(cancel) != 0)
    {
      close(gpio);
      return -1;
    }

  signaled = 0;
  sigemptyset(&action.sa_mask);
  if (sigaction(SIGUSR1, &action, &previous) != 0)
    {
      goto out;
    }

  if (gpio_resource_irq_check(gpio) != 0 || gpio_resource_irq_read(gpio) != 0
      || acquisition_event_tick(cancel[0], 0) != 1
      || check_wait(gpio, cancel[0], cancel[1]) != 0
      || read(cancel[0], &byte, 1) != 1 || byte != 'c'
      || check_wait(gpio, cancel[0], -1) != 0
      || gpio_resource_irq_register(gpio) != 0)
    {
      goto restore_signal;
    }

  registered = 1;
  if (acquisition_event_wait(gpio, cancel[0]) != 1 || gpio_resource_irq_read(gpio) != 1
      || write(cancel[1], "c", 1) != 1
      || acquisition_event_wait(gpio, cancel[0]) != 0)
    {
      goto restore_signal;
    }

  result = 0;

restore_signal:
  if (sigaction(SIGUSR1, &previous, NULL) != 0)
    {
      result = -1;
    }

out:
  if (registered && (gpio_resource_irq_unregister(gpio) != 0 || gpio_resource_irq_read(gpio) != 0))
    {
      result = -1;
    }

  close(cancel[0]);
  close(cancel[1]);
  close(gpio);
  return result;
}

int main(int argc, char *argv[])
{
  (void)argc;
  (void)argv;
  int i2c = imu_witness_i2c();
  if (i2c != 0)
    {
      printf("S8_IMU_PROTECTED_ABI_FAIL i2c=%d\n", i2c);
      return 1;
    }

  if (check_gpio() != 0)
    {
      puts("S8_IMU_PROTECTED_ABI_FAIL gpio");
      return 1;
    }

  puts("S8_IMU_PROTECTED_ABI_PASS");
  return 0;
}
