/* SPDX-License-Identifier: MIT
 * Mechanical translations using the configured NuttX video ABI. Resource
 * ownership, allocation, validation and retry policy belong to Rust.
 */
#include <nuttx/config.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/videoio.h>
#include "status.h"
#include "v4l2.h"

int camera_open_flags(void)
{
  return O_NONBLOCK;
}

int camera_format(int fd, uint16_t width, uint16_t height,
                       uint8_t pixels, uint32_t *actual_width,
                       uint32_t *actual_height, uint8_t *actual_pixels)
{
  struct v4l2_format format = {0};
  uint32_t expected;

  if (!actual_width || !actual_height || !actual_pixels) return -5;
  if (pixels == 1) expected = V4L2_PIX_FMT_RGB565;
  else if (pixels == 2) expected = V4L2_PIX_FMT_JPEG;
  else if (pixels == 3) expected = V4L2_PIX_FMT_RGB565X;
  else return -1;

  format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  format.fmt.pix.width = width;
  format.fmt.pix.height = height;
  format.fmt.pix.pixelformat = expected;
  format.fmt.pix.field = V4L2_FIELD_NONE;
  if (ioctl(fd, VIDIOC_S_FMT, (unsigned long)(uintptr_t)&format) < 0)
    return rc_errno(errno);
  *actual_width = format.fmt.pix.width;
  *actual_height = format.fmt.pix.height;
  switch (format.fmt.pix.pixelformat)
    {
      case V4L2_PIX_FMT_RGB565: *actual_pixels = 1; break;
      case V4L2_PIX_FMT_JPEG: *actual_pixels = 2; break;
      case V4L2_PIX_FMT_RGB565X: *actual_pixels = 3; break;
      default: *actual_pixels = UINT8_MAX; break;
    }
  return 0;
}

int camera_quality(int fd, uint8_t quality)
{
  struct v4l2_control control = {0};
  if (quality < 1 || quality > 100) return -5;
  control.id = V4L2_CID_JPEG_COMPRESSION_QUALITY;
  control.value = quality;
  if (ioctl(fd, VIDIOC_S_CTRL, (unsigned long)(uintptr_t)&control) < 0)
    return rc_errno(errno);
  return 0;
}

int camera_buffers(int fd, uint32_t count)
{
  struct v4l2_requestbuffers request = {0};
  request.count = count;
  request.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  request.memory = V4L2_MEMORY_USERPTR;
  request.mode = V4L2_BUF_MODE_FIFO;
  if (ioctl(fd, VIDIOC_REQBUFS, (unsigned long)(uintptr_t)&request) < 0)
    return rc_errno(errno);
  return request.count == count ? 0 : -5;
}

int camera_queue(int fd, uint32_t index, uint8_t *data, size_t length)
{
  struct v4l2_buffer buffer = {0};
  if (length > UINT32_MAX) return -5;
  buffer.index = index;
  buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  buffer.memory = V4L2_MEMORY_USERPTR;
  buffer.m.userptr = (unsigned long)(uintptr_t)data;
  buffer.length = (uint32_t)length;
  if (ioctl(fd, VIDIOC_QBUF, (unsigned long)(uintptr_t)&buffer) < 0)
    return rc_errno(errno);
  return 0;
}

int camera_dequeue(int fd, uint32_t *index, uintptr_t *address,
                        uint32_t *capacity, uint32_t *bytes, uint32_t *flags)
{
  struct v4l2_buffer buffer = {0};
  if (!index || !address || !capacity || !bytes || !flags) return -5;
  buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  buffer.memory = V4L2_MEMORY_USERPTR;
  if (ioctl(fd, VIDIOC_DQBUF, (unsigned long)(uintptr_t)&buffer) < 0)
    return errno == EAGAIN || errno == EINTR ? 1 : rc_errno(errno);
  if (buffer.memory != V4L2_MEMORY_USERPTR) return -5;
  *index = buffer.index;
  *address = (uintptr_t)buffer.m.userptr;
  *capacity = buffer.length;
  *bytes = buffer.bytesused;
  *flags = (buffer.flags & V4L2_BUF_FLAG_ERROR) != 0;
  return 0;
}

int camera_stream(int fd, int on)
{
  enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (ioctl(fd, on ? VIDIOC_STREAMON : VIDIOC_STREAMOFF,
            (unsigned long)(uintptr_t)&type) < 0)
    return rc_errno(errno);
  return 0;
}
