/* Zephyr's documented weak early-memory hooks must work before RAM-function
 * initialization. Volatile accesses keep the compiler from calling the normal
 * memcpy/memset, whose measured implementations are deliberately in SRAM. */
#include <stddef.h>
#include <stdint.h>

void arch_early_memset(void *dst, int value, size_t bytes)
{
    volatile uint8_t *out = dst;
    while (bytes--) { *out++ = (uint8_t)value; }
}

void arch_early_memcpy(void *dst, const void *src, size_t bytes)
{
    volatile uint8_t *out = dst;
    const volatile uint8_t *in = src;
    while (bytes--) { *out++ = *in++; }
}
