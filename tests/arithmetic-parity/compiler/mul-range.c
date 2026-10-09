#include <stdbool.h>
#include <stdint.h>

/* Same observable overflow flag and zero fallback as the Rust reproducer. */
int64_t checked_unsigned_halves(uint32_t a, uint32_t b, bool *flag)
{
    int64_t result;
    *flag = __builtin_mul_overflow((int64_t)a, (int64_t)b, &result);
    return *flag ? 0 : result;
}

int64_t checked_masked_halves(uint64_t a, uint64_t b, bool *flag)
{
    int64_t result;
    *flag = __builtin_mul_overflow((int64_t)(a & UINT32_MAX),
                                  (int64_t)(b & UINT32_MAX), &result);
    return *flag ? 0 : result;
}

int64_t checked_full_width(int64_t a, int64_t b, bool *flag)
{
    int64_t result;
    *flag = __builtin_mul_overflow(a, b, &result);
    return *flag ? 0 : result;
}
