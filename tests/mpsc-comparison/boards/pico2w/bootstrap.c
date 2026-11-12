/* The reset path runs before the SRAM functions have been copied. Volatile
 * accesses keep the compiler from turning these loops into calls to libc. */
#include <stddef.h>
#include <stdint.h>

void *mp_boot_memset(void *dst, int value, size_t bytes)
{
    volatile uint8_t *out = dst;
    while (bytes--) { *out++ = (uint8_t)value; }
    return dst;
}

void *mp_boot_memcpy(void *dst, const void *src, size_t bytes)
{
    volatile uint8_t *out = dst;
    const volatile uint8_t *in = src;
    while (bytes--) { *out++ = *in++; }
    return dst;
}

void arch_early_memset(void *dst, int value, size_t bytes)
{
    mp_boot_memset(dst, value, bytes);
}

void arch_early_memcpy(void *dst, const void *src, size_t bytes)
{
    mp_boot_memcpy(dst, src, bytes);
}
