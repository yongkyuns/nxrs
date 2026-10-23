/* Actual SDK header layouts, constants and inline attribute operations only. */
#include <stddef.h>
#include <stdint.h>
#include <mbedtls/build_info.h>
#include <mbedtls/pk.h>
#include <mbedtls/x509_csr.h>
#include <psa/crypto.h>
#include <psa/crypto_extra.h>

#if MBEDTLS_VERSION_NUMBER != 0x04010100 || TF_PSA_CRYPTO_VERSION_NUMBER != 0x01010100
#error "Identity qualification requires the pinned Mbed TLS 4.1.1 / TF-PSA-Crypto 1.1.1 headers"
#endif
#if !defined(MBEDTLS_HMAC_DRBG_C) || defined(MBEDTLS_CTR_DRBG_C) || \
    !defined(MBEDTLS_PSA_DRIVER_GET_ENTROPY) || defined(MBEDTLS_PSA_BUILTIN_GET_ENTROPY) || \
    defined(MBEDTLS_PSA_CRYPTO_EXTERNAL_RNG) || defined(MBEDTLS_ENTROPY_NV_SEED) || \
    defined(MBEDTLS_ENTROPY_NO_SOURCES_OK) || defined(MBEDTLS_PSA_CRYPTO_STORAGE_C) || \
    defined(MBEDTLS_PSA_ITS_FILE_C) || defined(MBEDTLS_PSA_KEY_STORE_DYNAMIC) || \
    defined(MBEDTLS_THREADING_C) || defined(MBEDTLS_PSA_CRYPTO_KEY_ID_ENCODES_OWNER)
#error "Unsupported identity entropy, key storage or concurrency configuration"
#endif
_Static_assert(sizeof(psa_status_t) == sizeof(int), "PSA status ABI");
_Static_assert(sizeof(mbedtls_md_type_t) == sizeof(int), "SDK hash enum ABI");
_Static_assert(sizeof(mbedtls_svc_key_id_t) == sizeof(uint32_t), "PSA key identifier ABI");
_Static_assert(MBEDTLS_PSA_KEY_SLOT_COUNT == 4, "bounded identity key slots");
_Static_assert(MBEDTLS_PSA_RNG_RESEED_INTERVAL == 1024, "identity reseed policy");

size_t nxrs_di_attributes_size(void) { return sizeof(psa_key_attributes_t); }
size_t nxrs_di_attributes_align(void) { return _Alignof(psa_key_attributes_t); }
void nxrs_di_attributes_init(void *p) { *(psa_key_attributes_t *)p = psa_key_attributes_init(); }
void nxrs_di_set_type(void *p, uint16_t value) { psa_set_key_type(p, value); }
void nxrs_di_set_bits(void *p, size_t value) { psa_set_key_bits(p, value); }
void nxrs_di_set_usage(void *p, uint32_t value) { psa_set_key_usage_flags(p, value); }
void nxrs_di_set_algorithm(void *p, uint32_t value) { psa_set_key_algorithm(p, value); }
void nxrs_di_set_lifetime(void *p, uint32_t value) { psa_set_key_lifetime(p, value); }
uint16_t nxrs_di_p256_type(void) { return PSA_KEY_TYPE_ECC_KEY_PAIR(PSA_ECC_FAMILY_SECP_R1); }
uint32_t nxrs_di_sign_hash_usage(void) { return PSA_KEY_USAGE_SIGN_HASH; }
uint32_t nxrs_di_ecdsa_sha256(void) { return PSA_ALG_ECDSA(PSA_ALG_SHA_256); }
uint32_t nxrs_di_volatile_lifetime(void) { return PSA_KEY_LIFETIME_VOLATILE; }
int nxrs_di_sha256(void) { return MBEDTLS_MD_SHA256; }
int nxrs_di_entropy_error(void) { return PSA_ERROR_INSUFFICIENT_ENTROPY; }
size_t nxrs_di_pk_size(void) { return sizeof(mbedtls_pk_context); }
size_t nxrs_di_pk_align(void) { return _Alignof(mbedtls_pk_context); }
size_t nxrs_di_csr_size(void) { return sizeof(mbedtls_x509write_csr); }
size_t nxrs_di_csr_align(void) { return _Alignof(mbedtls_x509write_csr); }
