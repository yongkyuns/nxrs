/* SDK qualification only: actual export flag and entropy callback declaration. */
#include <stdint.h>
#include <psa/crypto.h>
#include <mbedtls/platform.h>
#include <mbedtls/platform_time.h>
int tls_test_clock_probe(int64_t expected)
{
  mbedtls_time_t output = 0;
  mbedtls_time_t value = mbedtls_time(&output);
  return value == expected && output == expected ? 0 : -1;
}
uint32_t tls_test_export_usage(void)
{
  return PSA_KEY_USAGE_SIGN_HASH | PSA_KEY_USAGE_EXPORT;
}
int tls_test_entropy_probe(size_t *bits, uint8_t *output, size_t size)
{
  return mbedtls_platform_get_entropy(0, bits, output, size);
}
