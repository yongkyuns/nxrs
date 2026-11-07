/* Fixture-only worker identity witness. */
#include <pthread.h>
#include <stdint.h>

int32_t imu_qualification_worker_tid(void)
{
  return (int32_t)pthread_gettid_np(pthread_self());
}
