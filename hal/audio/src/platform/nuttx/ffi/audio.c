#include <nuttx/config.h>
#include <nuttx/audio/audio.h>

#include <errno.h>
#include <fcntl.h>
#include <mqueue.h>
#include <poll.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/ioctl.h>

#if !defined(CONFIG_ARCH_CHIP_ESP32S3) || !defined(CONFIG_AUDIO_I2S) || \
    !defined(CONFIG_ESPRESSIF_I2S0_RX) || \
    !defined(CONFIG_ESPRESSIF_I2S0_ROLE_MASTER) || \
    !defined(CONFIG_ESPRESSIF_I2S0_DATA_BIT_WIDTH_32BIT) || \
    CONFIG_ESPRESSIF_I2S0_SAMPLE_RATE != 16000 || \
    defined(CONFIG_ESPRESSIF_I2S0_TX) || defined(CONFIG_AUDIO_MULTI_SESSION) || \
    defined(CONFIG_AUDIO_EXCLUDE_STOP)
#  error "Microphone capture requires the receive-only ESP32-S3 audio configuration"
#endif

_Static_assert(CONFIG_ESPRESSIF_I2S_MAXINFLIGHT >= 4,
               "Keep room for new and completing audio buffers");
_Static_assert(CONFIG_MQ_MAXMSGSIZE >= sizeof(struct audio_msg_s),
               "The message queue must fit a native audio notification");

/* Keep target errno values private. One means retryable. */

static int status(int result)
{
  if (result >= 0)
    {
      return 0;
    }

  return errno == EAGAIN || errno == EBUSY || errno == EINTR ? 1 : -1;
}

int audio_capture_queue(const char *name, unsigned int capacity)
{
  struct mq_attr attr = {0};
  attr.mq_maxmsg = capacity;
  attr.mq_msgsize = sizeof(struct audio_msg_s);
  return mq_open(name, O_RDWR | O_CREAT | O_EXCL | O_NONBLOCK, 0600, &attr);
}

int audio_capture_unlink(const char *name)
{
  return status(mq_unlink(name));
}

int audio_capture_reserve(int fd)
{
  return status(ioctl(fd, AUDIOIOC_RESERVE, 0ul));
}

int audio_capture_release(int fd)
{
  return status(ioctl(fd, AUDIOIOC_RELEASE, 0ul));
}

int audio_capture_configure(int fd, uint32_t rate)
{
  struct audio_caps_desc_s desc = {0};
  desc.caps.ac_len = sizeof(desc.caps);
  desc.caps.ac_type = AUDIO_TYPE_INPUT;
  desc.caps.ac_subtype = AUDIO_FMT_PCM;
  desc.caps.ac_format.b[0] = AUDIO_SUBFMT_PCM_S32_LE;
  desc.caps.ac_channels = 2;
  desc.caps.ac_controls.hw[0] = rate & 0xffffu;
  desc.caps.ac_controls.b[2] = 32;
  desc.caps.ac_controls.b[3] = rate >> 16;
  return status(ioctl(fd, AUDIOIOC_CONFIGURE, (unsigned long)&desc));
}

int audio_capture_subscribe(int fd, int queue)
{
  return status(ioctl(fd, AUDIOIOC_REGISTERMQ, (unsigned long)queue));
}

int audio_capture_unsubscribe(int fd)
{
  return status(ioctl(fd, AUDIOIOC_UNREGISTERMQ, 0ul));
}

int audio_capture_start(int fd)
{
  return status(ioctl(fd, AUDIOIOC_START, 0ul));
}

int audio_capture_stop(int fd)
{
  return status(ioctl(fd, AUDIOIOC_STOP, 0ul));
}

int audio_capture_alloc(int fd, uint16_t bytes, uintptr_t *buffer)
{
  /* NuttX allocates the samples inline after the APB header. Reserve padding
   * so the sample pointer and size meet ESP32-S3 external GDMA alignment.
   * FREEBUFFER still receives the original APB allocation.
   */

  struct ap_buffer_s *apb = NULL;
  struct audio_buf_desc_s desc = {0};
  int ret;

  if (bytes == 0 || bytes > UINT16_MAX - 63 || (bytes & 63) != 0)
    {
      return -1;
    }

  desc.numbytes = bytes + 63;
  desc.u.pbuffer = &apb;
  ret = status(ioctl(fd, AUDIOIOC_ALLOCBUFFER, (unsigned long)&desc));
  if (ret != 0)
    {
      return ret;
    }

  if (apb == NULL)
    {
      return -1;
    }

  apb->samp = (uint8_t *)(((uintptr_t)apb->samp + 63) & ~(uintptr_t)63);
  apb->nmaxbytes = bytes;
  *buffer = (uintptr_t)apb;
  return 0;
}

int audio_capture_free(int fd, uintptr_t buffer)
{
  struct audio_buf_desc_s desc = {0};
  desc.u.buffer = (struct ap_buffer_s *)buffer;
  return status(ioctl(fd, AUDIOIOC_FREEBUFFER, (unsigned long)&desc));
}

int audio_capture_submit(int fd, uintptr_t buffer)
{
  struct audio_buf_desc_s desc = {0};
  struct ap_buffer_s *apb = (struct ap_buffer_s *)buffer;
  apb->curbyte = 0;
  apb->nbytes = 0;
  apb->flags &= ~(AUDIO_APB_FINAL | AUDIO_APB_DEQUEUED);
  desc.u.buffer = apb;
  return status(ioctl(fd, AUDIOIOC_ENQUEUEBUFFER, (unsigned long)&desc));
}

int audio_capture_poll(int queue, int milliseconds)
{
  struct pollfd pfd = {.fd = queue, .events = POLLIN};
  int ret = poll(&pfd, 1, milliseconds);
  if (ret < 0)
    {
      return errno == EINTR ? 2 : -1;
    }

  if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))
    {
      return -1;
    }

  return (pfd.revents & POLLIN) != 0;
}

int audio_capture_event(int queue, uintptr_t *buffer)
{
  struct audio_msg_s message;
  ssize_t size = mq_receive(queue, (char *)&message, sizeof(message), NULL);
  if (size < 0)
    {
      return errno == EAGAIN || errno == EINTR ? 2 : -1;
    }

  if ((size_t)size != sizeof(message))
    {
      return -2;
    }

  switch (message.msg_id)
    {
      case AUDIO_MSG_DEQUEUE:
        *buffer = (uintptr_t)message.u.ptr;
        return 1;

      case AUDIO_MSG_COMPLETE:
        return 0;

      case AUDIO_MSG_IOERR:
        return -1;

      default:
        return -2;
    }
}

const uint8_t *audio_capture_samples(uintptr_t buffer, uint16_t bytes)
{
  const struct ap_buffer_s *apb = (const struct ap_buffer_s *)buffer;
  if (apb->curbyte != 0 || apb->nbytes != bytes || bytes > apb->nmaxbytes)
    {
      return NULL;
    }

  return apb->samp;
}
