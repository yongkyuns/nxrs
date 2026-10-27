/* SDK witness only: distinguish a real denied export from an inert export API. */
#include <stdint.h>
#include <psa/crypto.h>
#include <mbedtls/platform.h>
uint32_t nxrs_di_test_export_usage(void)
{
  return PSA_KEY_USAGE_SIGN_HASH | PSA_KEY_USAGE_EXPORT;
}

int nxrs_di_test_entropy_probe(size_t *bits, uint8_t *output, size_t size)
{
  return mbedtls_platform_get_entropy(0, bits, output, size);
}
