/* Compile the actual SDK startup hook with its NuttX configuration mapping. */
#include <assert.h>
typedef struct { int inst; int *rwdt_dev; } wdt_hal_context_t;
int RTCCNTL;
#define ESP_OK 0
#define RWDT_HAL_CONTEXT_DEFAULT() ((wdt_hal_context_t){0, &RTCCNTL})
static unsigned stops, unlocks, locks;
void wdt_hal_write_protect_disable(wdt_hal_context_t *ctx) { (void)ctx; ++unlocks; }
void wdt_hal_disable(wdt_hal_context_t *ctx) { (void)ctx; ++stops; }
void wdt_hal_write_protect_enable(wdt_hal_context_t *ctx) { (void)ctx; ++locks; }
static int (*hook)(void);
#define ESP_SYSTEM_INIT_FN(name, ...) \
  static int name(void); static int (*hook)(void) = name; static int name(void)

#include "config.inc"
#include "hook.inc"

int main(void)
{
  if (hook) assert(hook() == ESP_OK);
#ifdef CONFIG_ESP32S3_RWDT_INHERIT
  unsigned expected = 0;
#else
  unsigned expected = 1;
#endif
  return stops != expected || unlocks != expected || locks != expected;
}
