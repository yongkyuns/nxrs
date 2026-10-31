/* Actual bootloader and native MTD operations over a bounded NOR cipher model.
 * XOR stands in for the SDK cipher; factory tests separately use real AES-XTS.
 */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define ESP_OK 0
#define DRAM_ATTR
#define FLASH_BUFFER_SIZE 256
#define FLASH_SECTOR_SIZE 4096
#define FLASH_DEVICE_INTERNAL_FLASH 0
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define ALIGN_OFFSET(n,a) ((n) % (a))
#define ALIGN_DOWN(n,a) ((n) / (a) * (a))
#define ALIGN_UP(n,a) ALIGN_DOWN((n) + (a) - 1, a)
#define IS_ALIGNED(n,a) (ALIGN_OFFSET(n,a) == 0)
typedef int esp_err_t;
struct flash_area { uint32_t fa_device_id, fa_off, fa_size; };
typedef struct {
    void (*flash_encryption_enable)(void);
    void (*flash_encryption_disable)(void);
    void (*flash_encryption_data_prepare)(uint32_t, const uint32_t *, uint32_t);
    void (*flash_encryption_done)(void);
    void (*flash_encryption_destroy)(void);
    bool (*flash_encryption_check)(uint32_t, uint32_t);
} spi_flash_encryption_t;
static uint8_t flash[8192];
static int active, prepared, writes, erases, raw_error, decrypt_error, fail_at, dropped, erase_failed;
static int spi_flash_read(uint32_t addr, void *dest, uint32_t size)
{
    assert(addr <= sizeof(flash) && size <= sizeof(flash) - addr);
    if (raw_error) return -47;
    memcpy(dest, flash + addr, size);
    return 0;
}
static int spi_flash_read_encrypted(uint32_t addr, void *dest, uint32_t size)
{
    assert(addr <= sizeof(flash) && size <= sizeof(flash) - addr);
    if (decrypt_error) return -47;
    for (uint32_t i = 0; i < size; i++) ((uint8_t *)dest)[i] = flash[addr+i] ^ 0xa5;
    return 0;
}
static int program(uint32_t addr, const void *data, uint32_t size)
{
    assert(addr <= sizeof(flash) && size <= sizeof(flash) - addr);
    writes++;
    if (writes == fail_at) return -47;
    if (!dropped) for (uint32_t i = 0; i < size; i++) flash[addr+i] &= ((const uint8_t *)data)[i] ^ 0xa5;
    return 0;
}
static int spi_flash_write(uint32_t addr, const void *data, uint32_t size)
{ assert(active && prepared); return program(addr, data, size); }
static int bootloader_flash_read(uint32_t addr, void *dest, uint32_t size, bool decrypt)
{ return decrypt ? spi_flash_read_encrypted(addr, dest, size) : spi_flash_read(addr, dest, size); }
static int bootloader_flash_write(uint32_t addr, void *data, uint32_t size, bool encrypt)
{ assert(encrypt); return program(addr, data, size); }
static int bootloader_flash_erase_range(uint32_t addr, uint32_t size)
{ erases++; if (!erase_failed) memset(flash + addr, 0xff, size); return 0; }
static void flush_cache(uint32_t addr, uint32_t size) { (void)addr; (void)size; }
static bool check(uint32_t addr, uint32_t size) { return !(addr % 16 || size % 16); }
static void enable(void) { assert(!active); active = 1; }
static void disable(void) { assert(active && !prepared); active = 0; }
static void prepare(uint32_t addr, const uint32_t *data, uint32_t size)
{ assert(active && !prepared && data && check(addr, size)); prepared = 1; }
static void done(void) { assert(active && prepared); }
static void destroy(void) { assert(active && prepared); prepared = 0; }
static spi_flash_encryption_t esp_flash_encryption_default = {enable, disable, prepare, done, destroy, check};
#include "boot_io.inc"
#include "native_read.inc"
#include "native_write.inc"
static const struct flash_area area = {0, 0, sizeof(flash)};
static int boot_write(uint32_t addr, const void *data, uint32_t size)
{ return flash_area_write(&area, addr, data, size); }
static int boot_read(uint32_t addr, void *data, uint32_t size)
{ return flash_area_read(&area, addr, data, size); }
static void reset(void)
{
    assert(!active && !prepared);
    memset(flash, 0xff, sizeof(flash));
    writes = erases = raw_error = decrypt_error = fail_at = dropped = erase_failed = 0;
}
int main(void)
{
    int (*writers[])(uint32_t, const void *, uint32_t) = {boot_write, spi_flash_write_encrypted_manu};
    int (*readers[])(uint32_t, void *, uint32_t) = {boot_read, spi_flash_read_erased_aware};
    uint8_t data[768], output[800], saved[32];
    memset(data, 0xff, sizeof(data)); data[0] = 1;
    for (unsigned int w = 0; w < 2; w++) {
        reset();
        assert(writers[w](0, data, 32) == 0);
        memcpy(saved, flash, 32);
        assert(writers[w](32, data, 32) == 0 && !memcmp(saved, flash, 32) && erases == 0);
        assert(writers[w](0, data, 32) < 0 && writes == 2);
        assert(writers[w](1, data, 32) < 0 && writes == 2);
        for (unsigned int r = 0; r < 2; r++) {
            memset(output, 0x33, sizeof(output));
            assert(readers[r](1, output, 767) == 0);
            assert(output[0] == 0xff && output[31] == 1 && output[767] == 0x33);
            for (unsigned int i = 32; i < 767; i++) assert(output[i] == 0xff);
            raw_error = 1; assert(readers[r](0, output, 32) < 0); raw_error = 0;
            decrypt_error = 1; assert(readers[r](0, output, 32) < 0); decrypt_error = 0;
        }
        erase_failed = 1; assert(flash_area_erase(&area, 0, 4096) < 0);
        erase_failed = 0; assert(flash_area_erase(&area, 0, 4096) == 0 && writes == 2);
        for (int failure = 1; failure <= 3; failure++) {
            reset(); fail_at = failure;
            assert(writers[w](0, data, sizeof(data)) < 0 && writes == failure && !active && !prepared && !erases);
        }
        reset(); dropped = 1;
        /* All-0xff writes must be verified through actual decryption, not erased normalization. */
        assert(writers[w](0, data + 32, 32) < 0 && !active && !prepared && !erases);
        reset(); raw_error = 1; assert(writers[w](0, data, 32) < 0 && !writes && !active);
        reset(); decrypt_error = 1; assert(writers[w](0, data, 32) < 0 && !active && !prepared);
    }
    reset();
    assert(flash_area_read(&area, UINT32_MAX, output, 32) < 0);
    assert(flash_area_write(&area, sizeof(flash) - 32, data, 64) < 0 && !writes);
    assert(flash_area_erase(&area, 1, 4096) < 0 && !erases);
    assert(flash_area_erase(&area, 4096, 8192) < 0 && !erases);
    return 0;
}
