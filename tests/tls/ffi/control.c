/* SDK qualification only: actual export flag and entropy callback declaration. */
#include <stdint.h>
#include <psa/crypto.h>
#include <mbedtls/platform.h>
uint32_t tls_test_export_usage(void)
{
  return PSA_KEY_USAGE_SIGN_HASH | PSA_KEY_USAGE_EXPORT;
}
int tls_test_entropy_probe(size_t *bits, uint8_t *output, size_t size)
{
  return mbedtls_platform_get_entropy(0, bits, output, size);
}
