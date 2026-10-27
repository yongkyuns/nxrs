/* Application qualification only: public RSA/PSS/SHA256 with standard allocation.
 * Separate from the bootloader's fixed-buffer allocator; no TLS/device RNG. */
#ifndef NXRS_ADMISSION_MBEDTLS_CONFIG_H
#define NXRS_ADMISSION_MBEDTLS_CONFIG_H
#define MBEDTLS_HAVE_ASM
#define MBEDTLS_BIGNUM_C
#define MBEDTLS_OID_C
#define MBEDTLS_RSA_C
#define MBEDTLS_PKCS1_V21
#define MBEDTLS_ASN1_PARSE_C
#define MBEDTLS_MD_C
#define MBEDTLS_SHA256_C
#endif
