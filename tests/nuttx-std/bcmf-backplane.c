/* Exercise the dependency's actual shared-window read/write helpers. Wi-Fi
 * and HCI use different addresses on one bus; window selection plus transfer
 * must be atomic. This does not simulate a radio or qualify coexistence.
 */
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define OK 0
#define CONFIG_IEEE80211_INFINEON_CYW43439_BLUETOOTH 1
#define SBSDIO_SB_OFT_ADDR_MASK 0x7fff
#define SBSDIO_SB_ACCESS_2_4B_FLAG 0x8000
typedef struct { pthread_mutex_t backplane_lock; } bcmf_interface_dev_t;
static bcmf_interface_dev_t bus = {PTHREAD_MUTEX_INITIALIZER};
static _Thread_local unsigned depth;
static _Thread_local uint32_t expected;
static uint32_t window;
static bool fail_window, fail_transfer;

static int nxmutex_lock(pthread_mutex_t *lock) {
  int result = pthread_mutex_lock(lock);
  assert(result == 0 && depth++ == 0);
  return 0;
}
static void nxmutex_unlock(pthread_mutex_t *lock) {
  assert(depth-- == 1 && pthread_mutex_unlock(lock) == 0);
}
static int bcmf_core_set_backplane_window(bcmf_interface_dev_t *b,
                                         uint32_t address) {
  assert(b == &bus && depth == 1);
  window = address & ~SBSDIO_SB_OFT_ADDR_MASK;
  sched_yield(); /* Invite the other caller between selection and transfer. */
  return fail_window ? -EIO : 0;
}
static int bcmf_transfer_bytes(bcmf_interface_dev_t *b, bool write,
                               unsigned function, uint32_t address,
                               uint8_t *data, unsigned length) {
  assert(b == &bus && depth == 1 && function == 1 && length == 4);
  assert(window == (expected & ~SBSDIO_SB_OFT_ADDR_MASK));
  assert(address == ((expected & SBSDIO_SB_OFT_ADDR_MASK) |
                     SBSDIO_SB_ACCESS_2_4B_FLAG));
  if (!write) memcpy(data, &expected, 4);
  return fail_transfer ? -EIO : 0;
}
#include "bcmf-backplane-functions.h"

static void *caller(void *argument) {
  expected = (uintptr_t)argument;
  for (unsigned i = 0; i < 1000; i++) {
    uint32_t data = expected;
    assert(bcmf_write_sbreg(&bus, expected, (uint8_t *)&data, 4) == 0);
    data = 0;
    assert(bcmf_read_sbreg(&bus, expected, (uint8_t *)&data, 4) == 0);
    assert(data == expected);
  }
  return NULL;
}
int main(void) {
  pthread_t callers[2];
  assert(pthread_create(&callers[0], NULL, caller, (void *)(uintptr_t)0x18001000) == 0);
  assert(pthread_create(&callers[1], NULL, caller, (void *)(uintptr_t)0x19011000) == 0);
  for (unsigned i = 0; i < 2; i++) assert(pthread_join(callers[i], NULL) == 0);
  expected = 0x18001000;
  uint32_t data = 0;
  for (unsigned stage = 0; stage < 2; stage++) {
    fail_window = stage == 0; fail_transfer = stage == 1;
    assert(bcmf_read_sbreg(&bus, expected, (uint8_t *)&data, 4) == -EIO);
    assert(bcmf_write_sbreg(&bus, expected, (uint8_t *)&data, 4) == -EIO);
    assert(depth == 0 && pthread_mutex_trylock(&bus.backplane_lock) == 0);
    assert(pthread_mutex_unlock(&bus.backplane_lock) == 0);
  }
  assert(pthread_mutex_destroy(&bus.backplane_lock) == 0);
  return 0;
}
