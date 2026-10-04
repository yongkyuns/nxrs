/* Qualification-only cycle clock. Valid for the pinned single-core 240 MHz
 * ESP32-S3 NuttX configuration; not a portable wall clock or production HAL.
 */
#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/sched.h>
#include <errno.h>
#include <fcntl.h>
#include <malloc.h>
#include <mqueue.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>

#ifndef CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ
#  error "ESP32-S3 cycle probe requires the ESP32-S3 target"
#endif
#if CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ != 240
#  error "ESP32-S3 cycle probe requires fixed 240 MHz CPU frequency"
#endif
#ifdef CONFIG_SMP
#  error "Per-core CCOUNT timestamps require a single-core configuration"
#endif

uint32_t nxrs_cq_cycles(void)
{
  uint32_t cycles;
  __asm__ volatile("rsr.ccount %0" : "=a"(cycles));
  return cycles;
}

/* Diagnostic twin of channel_scale_mq.c's full-payload checksum. Keep the
 * operation and byte count identical to isolate target compiler codegen. */
uint32_t nxrs_cq_checksum(uint8_t lane, uint8_t producer, uint16_t sequence,
                          const uint8_t *payload)
{
  uint32_t sum = (uint32_t)lane << 24 | (uint32_t)producer << 16 | sequence;
  for (unsigned i = 0; i < 236; i++)
    {
      sum = (sum << 3 | sum >> 29) + payload[i];
    }
  return sum;
}

/* Called from the current NuttX task before it exits. The platform profile
 * enables stack coloration, so this reports observed use, not reservation. */
uint32_t nxrs_cq_stack_hwm(void)
{
#ifdef CONFIG_STACK_COLORATION
  struct tcb_s *tcb = nxsched_self();
  return (uint32_t)up_check_tcbstack(tcb, tcb->adj_stack_size);
#else
  return 0;
#endif
}

uint32_t nxrs_cq_heap_used(void)
{
  return (uint32_t)mallinfo().uordblks;
}

/* Qualification-only nonblocking/full probe, used before any peer starts.
 * 0 = queued, 1 = full, -1 = error. Restore the descriptor's original flags
 * before a worker or other sender can use this queue. */
int nxrs_cq_mq_try_send(int fd, const void *message, size_t length)
{
  struct mq_attr attr;
  struct mq_attr previous;
  if (mq_getattr(fd, &attr) < 0)
    {
      return -1;
    }
  attr.mq_flags |= O_NONBLOCK;
  if (mq_setattr(fd, &attr, &previous) < 0)
    {
      return -1;
    }
  int result = mq_send(fd, message, length, 0);
  int send_error = errno;
  if (mq_setattr(fd, &previous, NULL) < 0)
    {
      return -1;
    }
  if (result == 0)
    {
      return 0;
    }
  return send_error == EAGAIN ? 1 : -1;
}

/* NuttX extends struct pollfd with private pointers. Rust owns an opaque,
 * caller-allocated buffer; C owns the layout and keeps pollfd entries stable
 * across waits, just like the direct C control. */
struct nxrs_cq_pollset
{
  struct pollfd fds[15];
  unsigned count;
  unsigned cursor;
};

_Static_assert(sizeof(struct nxrs_cq_pollset) <= 96 * sizeof(uint32_t),
               "Rust opaque poll storage is too small");
_Static_assert(_Alignof(struct nxrs_cq_pollset) <= _Alignof(uint32_t),
               "Rust opaque poll storage is under-aligned");

int nxrs_cq_pollset_init(uint32_t *storage, size_t bytes)
{
  if (!storage || bytes < sizeof(struct nxrs_cq_pollset))
    {
      return -1;
    }
  memset(storage, 0, sizeof(struct nxrs_cq_pollset));
  return 0;
}

int nxrs_cq_pollset_add(uint32_t *storage, int fd)
{
  struct nxrs_cq_pollset *set = (struct nxrs_cq_pollset *)storage;
  if (set->count >= 15 || fd < 0)
    {
      return -1;
    }
  unsigned index = set->count++;
  set->fds[index] = (struct pollfd){.fd = fd, .events = POLLIN};
  return (int)index;
}

void nxrs_cq_pollset_remove(uint32_t *storage, unsigned index)
{
  struct nxrs_cq_pollset *set = (struct nxrs_cq_pollset *)storage;
  if (index < set->count)
    {
      set->fds[index].fd = -1;
      set->fds[index].events = 0;
    }
}

int nxrs_cq_pollset_fd(const uint32_t *storage, unsigned index)
{
  const struct nxrs_cq_pollset *set = (const struct nxrs_cq_pollset *)storage;
  return index < set->count ? set->fds[index].fd : -1;
}

int nxrs_cq_pollset_select(uint32_t *storage)
{
  struct nxrs_cq_pollset *set = (struct nxrs_cq_pollset *)storage;
  if (set->count == 0 || set->cursor >= set->count)
    {
      return -1;
    }
  for (;;)
    {
      int ready = poll(set->fds, set->count, -1);
      if (ready < 0 && errno == EINTR)
        {
          continue;
        }
      if (ready <= 0)
        {
          return -1;
        }
      for (unsigned offset = 0; offset < set->count; offset++)
        {
          unsigned index = (set->cursor + offset) % set->count;
          if (set->fds[index].revents & POLLIN)
            {
              set->cursor = (index + 1) % set->count;
              return (int)index;
            }
          if (set->fds[index].revents & (POLLERR | POLLNVAL))
            {
              return -1;
            }
        }
    }
}
