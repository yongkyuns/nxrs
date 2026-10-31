/* Actual SDK startup prefix and burn function, over side-effect witnesses. */
#include <stdbool.h>
#include <setjmp.h>
#define CONFIG_SECURE_BOOT_REQUIRE_ALREADY_ENABLED 1
#define CONFIG_SECURE_FLASH_REQUIRE_ALREADY_ENABLED 1
#define CONFIG_EFUSE_WRITES_FORBIDDEN 1
#define ESP_OK 0
#define ESP_ERR_NOT_SUPPORTED 0x106
#define FIH_PANIC longjmp(halt, 1)
typedef int esp_err_t;
static jmp_buf halt;
static bool secure, encrypted;
static unsigned initializations, resets, pending;
static int init_result;
bool esp_secure_boot_enabled(void) { return secure; }
bool esp_flash_encryption_enabled(void) { return encrypted; }
static int bootloader_init(void) { ++initializations; return init_result; }
static void esp_efuse_utility_reset(void) { ++resets; pending = 0; }
#include "startup.inc"
#include "burn.inc"
int main(void)
{
  for (unsigned state = 0; state < 4; ++state) {
    secure = (state & 1) != 0;
    encrypted = (state & 2) != 0;
    initializations = 0;
    if (setjmp(halt) == 0) {
      if (boot_entry() != 0 || state != 3 || initializations != 1) return 1;
    } else if (state == 3 || initializations != 0) return 2;
  }
  initializations = 0;
  init_result = -1;
  if (setjmp(halt) == 0) { (void)boot_entry(); return 3; }
  if (initializations != 1) return 4;
  for (unsigned attempt = 0; attempt < 2; ++attempt) {
    pending = 0xff;
    if (esp_efuse_utility_burn_chip() != ESP_ERR_NOT_SUPPORTED || pending != 0) return 5;
  }
  return resets == 2 ? 0 : 6;
}
