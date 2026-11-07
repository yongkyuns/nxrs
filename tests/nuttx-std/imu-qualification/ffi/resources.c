/* Fixture-only scalar access to pinned NuttX CPU-clock and heap ABIs. */
#include <errno.h>
#include <malloc.h>
#include <pthread.h>
#include <stdint.h>
#include <sys/mount.h>
#include <time.h>
#include <sys/types.h>

#if !defined(CONFIG_SMP) && defined(CONFIG_SCHED_CRITMONITOR) && \
    CONFIG_SCHED_CRITMONITOR_MAXTIME_THREAD >= 0 && \
    defined(CONFIG_STACK_COLORATION) && defined(CONFIG_FS_PROCFS) && \
    !defined(CONFIG_FS_PROCFS_EXCLUDE_PROCESS) && \
    defined(CONFIG_ARCH_PERF_EVENTS) && defined(CONFIG_PERF_OVERFLOW_CORRECTION)
#  define IMU_RESOURCES_AVAILABLE
#endif

int imu_qualification_resources_available(void)
{
#ifdef IMU_RESOURCES_AVAILABLE
  return 1;
#else
  return 0;
#endif
}

int imu_qualification_mount_procfs(void)
{
#ifdef IMU_RESOURCES_AVAILABLE
  if (mount(NULL, "/proc", "procfs", 0, NULL) == 0)
    {
      return 0;
    }

  if (errno == EBUSY)
    {
      return 1;
    }

  return -1;
#else
  errno = ENOSYS;
  return -1;
#endif
}

int imu_qualification_resources(pid_t tid, uint64_t *cpu_ns,
                                int32_t heap_bytes[2])
{
#ifdef IMU_RESOURCES_AVAILABLE
  clockid_t clock_id;
  struct timespec cpu;
  struct mallinfo heap;
  int result;

  if (cpu_ns == NULL || heap_bytes == NULL || tid <= 0)
    {
      errno = EINVAL;
      return -1;
    }

  result = pthread_getcpuclockid((pthread_t)tid, &clock_id);
  if (result != 0)
    {
      errno = result;
      return -1;
    }

  if (clock_gettime(clock_id, &cpu) < 0)
    {
      return -1;
    }

  if (cpu.tv_sec < 0 || cpu.tv_nsec < 0 || cpu.tv_nsec >= 1000000000L)
    {
      errno = EOVERFLOW;
      return -1;
    }

  if ((uint64_t)cpu.tv_sec > (UINT64_MAX - (uint64_t)cpu.tv_nsec) / 1000000000ULL)
    {
      errno = EOVERFLOW;
      return -1;
    }

  heap = mallinfo();
  if (heap.uordblks < 0 || heap.usmblks < 0)
    {
      errno = EOVERFLOW;
      return -1;
    }

  *cpu_ns = (uint64_t)cpu.tv_sec * 1000000000ULL + (uint64_t)cpu.tv_nsec;
  heap_bytes[0] = (int32_t)heap.uordblks;
  heap_bytes[1] = (int32_t)heap.usmblks;
  return 0;
#else
  (void)tid;
  (void)cpu_ns;
  (void)heap_bytes;
  errno = ENOSYS;
  return -1;
#endif
}
