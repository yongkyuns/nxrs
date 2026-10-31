/* Execute the native SDK function with failed SPI programs and cleanup spies. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#define DRAM_ATTR
#define ESP_OK 0
#define ESP_ERR_NOT_SUPPORTED 0x106
typedef int esp_err_t;
typedef struct {
    void (*flash_encryption_enable)(void);
    void (*flash_encryption_disable)(void);
    void (*flash_encryption_data_prepare)(uint32_t, const uint32_t *, uint32_t);
    void (*flash_encryption_done)(void);
    void (*flash_encryption_destroy)(void);
    bool (*flash_encryption_check)(uint32_t, uint32_t);
} spi_flash_encryption_t;
static int active, prepared, writes, destroyed, disabled, fail_at, failure;
static bool check(uint32_t address, uint32_t size) { return !(address % 16 || size % 16); }
static void enable(void) { assert(!active); active = 1; }
static void disable(void) { assert(active && !prepared); active = 0; disabled++; }
static void prepare(uint32_t address, const uint32_t *data, uint32_t size)
{ assert(active && !prepared && data && check(address, size)); prepared = 1; }
static void done(void) { assert(active && prepared); }
static void destroy(void) { assert(active && prepared); prepared = 0; destroyed++; }
static int spi_flash_write(uint32_t address, const void *data, uint32_t size)
{
    assert(active && prepared && data && check(address, size));
    writes++;
    return writes == fail_at ? failure : ESP_OK;
}
static spi_flash_encryption_t esp_flash_encryption_default =
    { enable, disable, prepare, done, destroy, check };
#include "encrypted_write.inc"
int main(void)
{
    uint32_t data[28] = {0}; /* 64-, 32- and 16-byte hardware programs. */
    if (spi_flash_write_encrypted_manu(1, data, sizeof(data)) != ESP_ERR_NOT_SUPPORTED ||
        active || writes || disabled) return 1;
    for (fail_at = 0; fail_at <= 3; fail_at++) {
        writes = destroyed = disabled = 0;
        failure = -100 - fail_at;
        int result = spi_flash_write_encrypted_manu(0, data, sizeof(data));
        if (result != (fail_at ? failure : ESP_OK) || writes != (fail_at ? fail_at : 3) ||
            destroyed != writes || disabled != 1 || active || prepared) return 1;
    }
    return 0;
}
