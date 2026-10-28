/* SPDX-License-Identifier: MIT
 * Synthetic sensor ONLY. All driver dispatch, tasks, locks, semaphores,
 * timers, descriptors, filesystem and UDP operations below are NuttX code.
 */
#include <nuttx/config.h>
#include <nuttx/fs/fs.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/stat.h>
#include <netinet/in.h>
#include <pthread.h>
#include <semaphore.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "bridge.h"

struct sensor
{
  pthread_t thread;
  pthread_mutex_t lock;
  sem_t request;
  int stopping;
  int requested;
  int ready;
  unsigned int sequence;
  uint8_t frame[4];
};

static pthread_mutex_t g_stats_lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned int g_opened;
static unsigned int g_closed;

static void *produce(void *argument)
{
  struct sensor *sensor = argument;
  for (;;)
    {
      while (sem_wait(&sensor->request) < 0)
        if (errno != EINTR) return NULL;
      pthread_mutex_lock(&sensor->lock);
      if (sensor->stopping)
        {
          pthread_mutex_unlock(&sensor->lock);
          break;
        }
      pthread_mutex_unlock(&sensor->lock);
      while (usleep(1000) < 0)
        if (errno != EINTR) return NULL;
      pthread_mutex_lock(&sensor->lock);
      sensor->sequence++;
      for (unsigned int i = 0; i < 4; i++)
        sensor->frame[i] = (uint8_t)(sensor->sequence + i);
      sensor->ready = 1;
      sensor->requested = 0;
      pthread_mutex_unlock(&sensor->lock);
    }
  return NULL;
}

static int sensor_open(struct file *filep)
{
  struct sensor *sensor = calloc(1, sizeof(*sensor));
  int result;
  if (!sensor) return -ENOMEM;
  result = pthread_mutex_init(&sensor->lock, NULL);
  if (result != 0) { free(sensor); return -result; }
  if (sem_init(&sensor->request, 0, 0) < 0)
    {
      result = -errno;
      pthread_mutex_destroy(&sensor->lock);
      free(sensor);
      return result;
    }
  result = pthread_create(&sensor->thread, NULL, produce, sensor);
  if (result != 0)
    {
      sem_destroy(&sensor->request);
      pthread_mutex_destroy(&sensor->lock);
      free(sensor);
      return -result;
    }
  filep->f_priv = sensor;
  pthread_mutex_lock(&g_stats_lock);
  g_opened++;
  pthread_mutex_unlock(&g_stats_lock);
  return 0;
}

static int sensor_close(struct file *filep)
{
  struct sensor *sensor = filep->f_priv;
  int result;
  pthread_mutex_lock(&sensor->lock);
  sensor->stopping = 1;
  pthread_mutex_unlock(&sensor->lock);
  sem_post(&sensor->request);
  result = pthread_join(sensor->thread, NULL);
  if (result != 0) return -result;
  sem_destroy(&sensor->request);
  pthread_mutex_destroy(&sensor->lock);
  free(sensor);
  filep->f_priv = NULL;
  pthread_mutex_lock(&g_stats_lock);
  g_closed++;
  pthread_mutex_unlock(&g_stats_lock);
  return 0;
}

static ssize_t sensor_read(struct file *filep, char *buffer, size_t length)
{
  struct sensor *sensor = filep->f_priv;
  ssize_t result;
  if (length < 4) return -ENOSPC;
  pthread_mutex_lock(&sensor->lock);
  if (sensor->ready)
    {
      memcpy(buffer, sensor->frame, 4);
      sensor->ready = 0;
      result = 4;
    }
  else if (sensor->sequence == 4)
    result = 0;
  else
    {
      if (!sensor->requested)
        {
          sensor->requested = 1;
          sem_post(&sensor->request);
        }
      result = -EAGAIN;
    }
  pthread_mutex_unlock(&sensor->lock);
  return result;
}

static int sensor_ioctl(struct file *filep, int command, unsigned long argument)
{
  struct rc_device_format *format = (struct rc_device_format *)(uintptr_t)argument;
  (void)filep;
  if (command != RCIOC_FORMAT) return -ENOTTY;
  if (!format) return -EINVAL;
  format->width = 2;
  format->height = 2;
  format->pixels = 0;
  return 0;
}

static const struct file_operations g_sensor_ops =
{
  .open = sensor_open,
  .close = sensor_close,
  .read = sensor_read,
  .ioctl = sensor_ioctl,
};

#ifdef NXRS_STD_ENTRY
extern int rc_rust_std_main(int argc, char *argv[]);
#else
extern int rc_rust_run(const char *output, uint16_t port);
#endif

void rc_sim_note(uint32_t phase)
{
  printf("RC_CHECK phase=%lu OK\n", (unsigned long)phase);
}

void rc_sim_panic(const uint8_t *file, size_t length, uint32_t line)
{
  printf("RC_NUTTX_SIM FAIL Rust panic %.*s:%lu\n", (int)length,
         (const char *)file, (unsigned long)line);
  fflush(stdout);
  abort();
}

struct application
{
  const char *path;
  uint16_t port;
  int result;
};

static void *run_rust(void *argument)
{
  struct application *app = argument;
#ifdef NXRS_STD_ENTRY
  char port[6];
  char *argv[4];
  snprintf(port, sizeof(port), "%u", (unsigned int)app->port);
  argv[0] = (char *)"nxrs";
  argv[1] = (char *)app->path;
  argv[2] = port;
  argv[3] = NULL;
  app->result = rc_rust_std_main(3, argv);
#else
  app->result = rc_rust_run(app->path, app->port);
#endif
  return NULL;
}

struct receiver
{
  int fd;
  int count;
  int error;
  uint8_t packets[4][28];
};

static void *receive_packets(void *argument)
{
  struct receiver *receiver = argument;
  for (int i = 0; i < 4; i++)
    {
      uint8_t packet[64];
      ssize_t length = recv(receiver->fd, packet, sizeof(packet), 0);
      if (length != 28) { receiver->error = 1; break; }
      memcpy(receiver->packets[i], packet, 28);
      receiver->count++;
    }
  return NULL;
}

static void print_hex(const char *kind, const uint8_t *data, size_t length)
{
  printf("%s ", kind);
  for (size_t i = 0; i < length; i++) printf("%02x", data[i]);
  printf("\n");
}

static int verify_records(const char *path)
{
  uint8_t record[44];
  const uint8_t expected[] = {1, 3, 4};
  int fd = open(path, O_RDONLY);
  int result = -1;
  if (fd < 0) return -1;
  for (int i = 0; i < 3; i++)
    {
      size_t offset = 0;
      while (offset < sizeof(record))
        {
          ssize_t n = read(fd, record + offset, sizeof(record) - offset);
          if (n <= 0) goto done;
          offset += (size_t)n;
        }
      if (memcmp(record, "RCAMREC1", 8) != 0 || record[16] != expected[i] ||
          record[32] != 4) goto done;
      for (int byte = 0; byte < 4; byte++)
        if (record[40 + byte] != expected[i] + byte) goto done;
      print_hex("RC_RECORD", record, sizeof(record));
    }
  if (read(fd, record, 1) != 0) goto done;
  result = 0;
done:
  if (close(fd) < 0) result = -1;
  return result;
}

int main(int argc, char *argv[])
{
  static int initialized;
  struct receiver receiver;
  struct application app;
  struct sockaddr_in address;
  struct timeval timeout = {3, 0};
  pthread_t receiver_thread;
#ifndef NXRS_STD_ENTRY
  pthread_t app_thread;
#endif
  socklen_t address_len = sizeof(address);
  unsigned int opened;
  unsigned int closed;
  int result = -1;
  int rc;

  if (argc > 1 && strcmp(argv[1], "fail") == 0)
    {
      printf("RC_NUTTX_SIM FAIL injected\n");
      return 1;
    }
  printf("RC_NUTTX_SIM BEGIN\n");
  puts("RC_NUTTX_STAGE initialize");
  if (!initialized)
    {
      if (mkdir("/rcam", 0700) < 0 && errno != EEXIST) goto fail;
      if (mount(NULL, "/rcam", "tmpfs", 0, NULL) < 0) goto fail;
      if (register_driver("/dev/nxrs-frame", &g_sensor_ops, 0444, NULL) < 0) goto fail;
      initialized = 1;
    }
  puts("RC_NUTTX_STAGE receiver-socket");
  memset(&receiver, 0, sizeof(receiver));
  receiver.fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (receiver.fd < 0) goto fail;
  memset(&address, 0, sizeof(address));
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  address.sin_port = 0;
  if (setsockopt(receiver.fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0 ||
      bind(receiver.fd, (struct sockaddr *)&address, sizeof(address)) < 0 ||
      getsockname(receiver.fd, (struct sockaddr *)&address, &address_len) < 0)
    goto close_socket;

  puts("RC_NUTTX_STAGE receiver-configured");
  pthread_mutex_lock(&g_stats_lock);
  opened = g_opened;
  closed = g_closed;
  pthread_mutex_unlock(&g_stats_lock);
  app.path = "/rcam/session.rcam";
  app.port = ntohs(address.sin_port);
  app.result = -1;
  puts("RC_NUTTX_STAGE receiver-thread");
  rc = pthread_create(&receiver_thread, NULL, receive_packets, &receiver);
  if (rc != 0) goto close_socket;
  puts("RC_NUTTX_STAGE application-entry");
#ifdef NXRS_STD_ENTRY
  /* Run rustc's generated std main on the NuttX command thread. The C code
   * remains only the synthetic-device/independent-verifier harness. */
  run_rust(&app);
#else
  rc = pthread_create(&app_thread, NULL, run_rust, &app);
  if (rc != 0) { pthread_join(receiver_thread, NULL); goto close_socket; }
  if (pthread_join(app_thread, NULL) != 0) abort();
#endif
  puts("RC_NUTTX_STAGE application-joined");
  if (pthread_join(receiver_thread, NULL) != 0) abort();
  puts("RC_NUTTX_STAGE receiver-joined");
  if (app.result != 0 || receiver.error || receiver.count != 4) goto close_socket;
  puts("RC_NUTTX_STAGE independent-record-check");
  if (verify_records(app.path) < 0) goto close_socket;
  for (int i = 0; i < receiver.count; i++)
    print_hex("RC_PACKET", receiver.packets[i], sizeof(receiver.packets[i]));
  pthread_mutex_lock(&g_stats_lock);
  opened = g_opened - opened;
  closed = g_closed - closed;
  pthread_mutex_unlock(&g_stats_lock);
  if (opened != 2 || closed != 2) goto close_socket;
  if (unlink(app.path) < 0) goto close_socket;
  result = 0;
close_socket:
  if (close(receiver.fd) < 0) result = -1;
  if (result == 0)
    {
      printf("RC_NUTTX_SIM PASS records=3 packets=4 opens=2 closes=2\n");
      return 0;
    }
fail:
  printf("RC_NUTTX_SIM FAIL errno=%d\n", errno);
  return 1;
}
