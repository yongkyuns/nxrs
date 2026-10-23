/* Actual SDK header layouts, constants and inline attribute operations only. */
#include <stddef.h>
#include <stdint.h>
#define MBEDTLS_DECLARE_PRIVATE_IDENTIFIERS
#include <mbedtls/build_info.h>
#include <mbedtls/pk.h>
#include <mbedtls/private/pk_private.h>
#include <mbedtls/x509_csr.h>
#include <mbedtls/x509_crt.h>
#include <mbedtls/x509_crl.h>
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
    defined(MBEDTLS_THREADING_C) || defined(MBEDTLS_PSA_CRYPTO_KEY_ID_ENCODES_OWNER) || \
    !defined(MBEDTLS_HAVE_TIME_DATE) || !defined(MBEDTLS_PLATFORM_TIME_MACRO) || \
    defined(MBEDTLS_PLATFORM_TIME_ALT) || defined(MBEDTLS_PLATFORM_STD_TIME)
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

/* Header-defined layouts/constants/field views only. Rust owns all policy. */
_Static_assert(sizeof(mbedtls_x509_time) == 6 * sizeof(int), "X.509 time ABI");
_Static_assert(offsetof(mbedtls_x509_time, year) == 0, "X.509 year ABI");
_Static_assert(offsetof(mbedtls_x509_time, mon) == sizeof(int), "X.509 month ABI");
_Static_assert(offsetof(mbedtls_x509_time, day) == 2 * sizeof(int), "X.509 day ABI");
_Static_assert(offsetof(mbedtls_x509_time, hour) == 3 * sizeof(int), "X.509 hour ABI");
_Static_assert(offsetof(mbedtls_x509_time, min) == 4 * sizeof(int), "X.509 minute ABI");
_Static_assert(offsetof(mbedtls_x509_time, sec) == 5 * sizeof(int), "X.509 second ABI");
_Static_assert(sizeof(mbedtls_x509_crt_profile) == 4 * sizeof(uint32_t), "X.509 profile ABI");
_Static_assert(offsetof(mbedtls_x509_crt_profile, allowed_mds) == 0, "X.509 profile hash ABI");
_Static_assert(offsetof(mbedtls_x509_crt_profile, allowed_pks) == 4, "X.509 profile key ABI");
_Static_assert(offsetof(mbedtls_x509_crt_profile, allowed_curves) == 8, "X.509 profile curve ABI");
_Static_assert(offsetof(mbedtls_x509_crt_profile, rsa_min_bitlen) == 12, "X.509 profile RSA ABI");
size_t nxrs_di_crt_size(void) { return sizeof(mbedtls_x509_crt); }
size_t nxrs_di_crt_align(void) { return _Alignof(mbedtls_x509_crt); }
size_t nxrs_di_crt_raw_length(const void *p) { return ((const mbedtls_x509_crt *)p)->raw.len; }
const void *nxrs_di_crt_next(const void *p) { return ((const mbedtls_x509_crt *)p)->next; }
const void *nxrs_di_crt_from(const void *p) { return &((const mbedtls_x509_crt *)p)->valid_from; }
const void *nxrs_di_crt_to(const void *p) { return &((const mbedtls_x509_crt *)p)->valid_to; }
void *nxrs_di_crt_pk(void *p) { return &((mbedtls_x509_crt *)p)->pk; }
int nxrs_di_crt_ca(const void *p) { return mbedtls_x509_crt_get_ca_istrue(p); }
int nxrs_di_crt_path_length(const void *p) { return ((const mbedtls_x509_crt *)p)->MBEDTLS_PRIVATE(max_pathlen); }
unsigned int nxrs_di_crt_usage(const void *p) { return ((const mbedtls_x509_crt *)p)->MBEDTLS_PRIVATE(key_usage); }
unsigned int nxrs_di_crt_extensions(const void *p) { return ((const mbedtls_x509_crt *)p)->MBEDTLS_PRIVATE(ext_types); }
const void *nxrs_di_crt_sans(const void *p) { return &((const mbedtls_x509_crt *)p)->subject_alt_names; }
const void *nxrs_di_crt_ekus(const void *p) { return &((const mbedtls_x509_crt *)p)->ext_key_usage; }
const void *nxrs_di_sequence_next(const void *p) { return ((const mbedtls_x509_sequence *)p)->next; }
const unsigned char *nxrs_di_sequence_data(const void *p) { return ((const mbedtls_x509_sequence *)p)->buf.p; }
size_t nxrs_di_sequence_length(const void *p) { return ((const mbedtls_x509_sequence *)p)->buf.len; }
int nxrs_di_sequence_tag(const void *p) { return ((const mbedtls_x509_sequence *)p)->buf.tag; }
unsigned int nxrs_di_usage_signature(void) { return MBEDTLS_X509_KU_DIGITAL_SIGNATURE; }
unsigned int nxrs_di_extension_constraints(void) { return MBEDTLS_X509_EXT_BASIC_CONSTRAINTS; }
unsigned int nxrs_di_extension_usage(void) { return MBEDTLS_X509_EXT_KEY_USAGE; }
unsigned int nxrs_di_extension_eku(void) { return MBEDTLS_X509_EXT_EXTENDED_KEY_USAGE; }
unsigned int nxrs_di_extension_san(void) { return MBEDTLS_X509_EXT_SUBJECT_ALT_NAME; }
int nxrs_di_san_uri_tag(void) { return MBEDTLS_ASN1_CONTEXT_SPECIFIC | MBEDTLS_X509_SAN_UNIFORM_RESOURCE_IDENTIFIER; }
int nxrs_di_san_ip_tag(void) { return MBEDTLS_ASN1_CONTEXT_SPECIFIC | MBEDTLS_X509_SAN_IP_ADDRESS; }
uint32_t nxrs_di_profile_sha256(void) { return MBEDTLS_X509_ID_FLAG(MBEDTLS_MD_SHA256); }
uint32_t nxrs_di_profile_ec_keys(void) { return MBEDTLS_X509_ID_FLAG(MBEDTLS_PK_ECDSA) | MBEDTLS_X509_ID_FLAG(MBEDTLS_PK_ECKEY); }
uint32_t nxrs_di_profile_p256(void) { return MBEDTLS_X509_ID_FLAG(MBEDTLS_ECP_DP_SECP256R1); }
uint32_t nxrs_di_psa_sha256(void) { return PSA_ALG_SHA_256; }

/* Mechanical optional-output/time_t translation for the native SDK callback. */
_Static_assert(sizeof(time_t) == sizeof(int64_t), "identity SDK time ABI");
_Static_assert((time_t)-1 < 0, "signed identity SDK time ABI");
time_t nxrs_di_sdk_time(time_t *output) {
  time_t value = (time_t)nxrs_di_trusted_utc();
  if (output != NULL) *output = value;
  return value;
}

/* CRL and certificate field views only; admission and state stay in Rust. */
_Static_assert(sizeof(mbedtls_pk_sigalg_t) == sizeof(int), "signature algorithm ABI");
_Static_assert(sizeof(psa_key_type_t) == sizeof(uint16_t), "public key type ABI");
const void *nxrs_di_crt_serial(const void *p) { return &((const mbedtls_x509_crt *)p)->serial; }
const void *nxrs_di_crt_subject(const void *p) { return &((const mbedtls_x509_crt *)p)->subject_raw; }
const void *nxrs_di_crt_ski(const void *p) { return &((const mbedtls_x509_crt *)p)->subject_key_id; }
const unsigned char *nxrs_di_buf_data(const void *p) { return ((const mbedtls_x509_buf *)p)->p; }
size_t nxrs_di_buf_length(const void *p) { return ((const mbedtls_x509_buf *)p)->len; }
size_t nxrs_di_crl_size(void) { return sizeof(mbedtls_x509_crl); }
size_t nxrs_di_crl_align(void) { return _Alignof(mbedtls_x509_crl); }
size_t nxrs_di_crl_raw_length(const void *p) { return ((const mbedtls_x509_crl *)p)->raw.len; }
const void *nxrs_di_crl_next(const void *p) { return ((const mbedtls_x509_crl *)p)->next; }
const void *nxrs_di_crl_from(const void *p) { return &((const mbedtls_x509_crl *)p)->this_update; }
const void *nxrs_di_crl_to(const void *p) { return &((const mbedtls_x509_crl *)p)->next_update; }
const void *nxrs_di_crl_tbs(const void *p) { return &((const mbedtls_x509_crl *)p)->tbs; }
const void *nxrs_di_crl_signature(const void *p) { return &((const mbedtls_x509_crl *)p)->MBEDTLS_PRIVATE(sig); }
const void *nxrs_di_crl_entries(const void *p) { return &((const mbedtls_x509_crl *)p)->entry; }
const void *nxrs_di_crl_entry_next(const void *p) { return ((const mbedtls_x509_crl_entry *)p)->next; }
const void *nxrs_di_crl_entry_serial(const void *p) { return &((const mbedtls_x509_crl_entry *)p)->serial; }
const void *nxrs_di_crl_entry_date(const void *p) { return &((const mbedtls_x509_crl_entry *)p)->revocation_date; }
unsigned int nxrs_di_usage_ca(void) { return MBEDTLS_X509_KU_KEY_CERT_SIGN | MBEDTLS_X509_KU_CRL_SIGN; }
uint16_t nxrs_di_p256_public_type(void) { return PSA_KEY_TYPE_ECC_PUBLIC_KEY(PSA_ECC_FAMILY_SECP_R1); }
int nxrs_di_signature_ecdsa(void) { return MBEDTLS_PK_SIGALG_ECDSA; }
