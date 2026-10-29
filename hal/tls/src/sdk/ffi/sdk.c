/* Actual SDK header layouts, constants and inline attribute operations only. */
#include <stddef.h>
#include <stdint.h>
#define MBEDTLS_DECLARE_PRIVATE_IDENTIFIERS
#include <mbedtls/build_info.h>
#include <mbedtls/pk.h>
#include <mbedtls/platform.h>
#include <mbedtls/private/pk_private.h>
#include <mbedtls/x509_csr.h>
#include <mbedtls/x509_crt.h>
#include <mbedtls/x509_crl.h>
#include <mbedtls/ssl.h>
#include <psa/crypto.h>
#include <psa/crypto_extra.h>

#if MBEDTLS_VERSION_NUMBER != 0x04020000 || TF_PSA_CRYPTO_VERSION_NUMBER != 0x01020000
#error "Requires official Mbed TLS 4.2.0 / TF-PSA-Crypto 1.2.0"
#endif
#if !defined(MBEDTLS_HMAC_DRBG_C) || defined(MBEDTLS_CTR_DRBG_C) || \
    !defined(MBEDTLS_PSA_DRIVER_GET_ENTROPY) || defined(MBEDTLS_PSA_BUILTIN_GET_ENTROPY) || \
    defined(MBEDTLS_PSA_CRYPTO_EXTERNAL_RNG) || defined(MBEDTLS_ENTROPY_NV_SEED) || \
    defined(MBEDTLS_ENTROPY_NO_SOURCES_OK) || defined(MBEDTLS_PSA_CRYPTO_STORAGE_C) || \
    defined(MBEDTLS_PSA_ITS_FILE_C) || defined(MBEDTLS_PSA_KEY_STORE_DYNAMIC) || \
    defined(MBEDTLS_THREADING_C) || defined(MBEDTLS_PSA_CRYPTO_KEY_ID_ENCODES_OWNER)
#error "Unsupported TLS identity entropy, storage or concurrency configuration"
#endif
_Static_assert(sizeof(psa_driver_get_entropy_flags_t) == sizeof(uint32_t), "entropy flags ABI");
_Static_assert(sizeof(psa_status_t) == sizeof(int), "PSA status ABI");
_Static_assert(sizeof(mbedtls_svc_key_id_t) == sizeof(uint32_t), "PSA key identifier ABI");
_Static_assert(MBEDTLS_PSA_KEY_SLOT_COUNT == 8, "bounded identity key slots");
_Static_assert(MBEDTLS_PSA_RNG_RESEED_INTERVAL == 1024, "identity reseed policy");
_Static_assert(sizeof(mbedtls_time_t) == sizeof(int64_t) && (mbedtls_time_t)-1 < 0,
               "Rust SDK time callback requires signed 64-bit time");
#if !defined(MBEDTLS_HAVE_TIME_DATE) || !defined(MBEDTLS_PLATFORM_TIME_MACRO) || \
    defined(MBEDTLS_PLATFORM_TIME_ALT) || defined(MBEDTLS_SSL_RENEGOTIATION) || \
    defined(MBEDTLS_SSL_SESSION_TICKETS) || defined(MBEDTLS_SSL_EARLY_DATA) || \
    defined(MBEDTLS_SSL_VARIABLE_BUFFER_LENGTH)
#error "Unsupported TLS clock, renegotiation or resumption configuration"
#endif

size_t tls_attributes_size(void) { return sizeof(psa_key_attributes_t); }
size_t tls_attributes_align(void) { return _Alignof(psa_key_attributes_t); }
void tls_attributes_init(void *p) { *(psa_key_attributes_t *)p = psa_key_attributes_init(); }
void tls_set_type(void *p, uint16_t value) { psa_set_key_type(p, value); }
void tls_set_bits(void *p, size_t value) { psa_set_key_bits(p, value); }
void tls_set_usage(void *p, uint32_t value) { psa_set_key_usage_flags(p, value); }
void tls_set_algorithm(void *p, uint32_t value) { psa_set_key_algorithm(p, value); }
void tls_set_lifetime(void *p, uint32_t value) { psa_set_key_lifetime(p, value); }
uint16_t tls_p256_type(void) { return PSA_KEY_TYPE_ECC_KEY_PAIR(PSA_ECC_FAMILY_SECP_R1); }
uint32_t tls_sign_hash_usage(void) { return PSA_KEY_USAGE_SIGN_HASH; }
uint32_t tls_ecdsa_sha256(void) { return PSA_ALG_ECDSA(PSA_ALG_SHA_256); }
uint32_t tls_volatile_lifetime(void) { return PSA_KEY_LIFETIME_VOLATILE; }
int tls_sha256(void) { return MBEDTLS_MD_SHA256; }
/* SDK enum width is compiler-specific (ARM GCC uses short enums). */
void tls_csr_set_md_alg(void *p, int algorithm)
{
  mbedtls_x509write_csr_set_md_alg(p, (mbedtls_md_type_t)algorithm);
}
int tls_entropy_error(void) { return PSA_ERROR_INSUFFICIENT_ENTROPY; }
size_t tls_pk_size(void) { return sizeof(mbedtls_pk_context); }
size_t tls_pk_align(void) { return _Alignof(mbedtls_pk_context); }
size_t tls_csr_size(void) { return sizeof(mbedtls_x509write_csr); }
size_t tls_csr_align(void) { return _Alignof(mbedtls_x509write_csr); }

size_t tls_ssl_size(void) { return sizeof(mbedtls_ssl_context); }
size_t tls_ssl_align(void) { return _Alignof(mbedtls_ssl_context); }
size_t tls_config_size(void) { return sizeof(mbedtls_ssl_config); }
size_t tls_config_align(void) { return _Alignof(mbedtls_ssl_config); }
size_t tls_crt_size(void) { return sizeof(mbedtls_x509_crt); }
size_t tls_crt_align(void) { return _Alignof(mbedtls_x509_crt); }
size_t tls_crl_size(void) { return sizeof(mbedtls_x509_crl); }
size_t tls_crl_align(void) { return _Alignof(mbedtls_x509_crl); }
int tls_server_endpoint(void) { return MBEDTLS_SSL_IS_SERVER; }
int tls_stream_transport(void) { return MBEDTLS_SSL_TRANSPORT_STREAM; }
int tls_default_preset(void) { return MBEDTLS_SSL_PRESET_DEFAULT; }
int tls_verify_required(void) { return MBEDTLS_SSL_VERIFY_REQUIRED; }
int tls_version12(void) { return MBEDTLS_SSL_VERSION_TLS1_2; }
int tls_version13(void) { return MBEDTLS_SSL_VERSION_TLS1_3; }
int tls_want_read(void) { return MBEDTLS_ERR_SSL_WANT_READ; }
int tls_want_write(void) { return MBEDTLS_ERR_SSL_WANT_WRITE; }
int tls_peer_closed(void) { return MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY; }
int tls_io_failure(void) { return MBEDTLS_ERR_SSL_INTERNAL_ERROR; }
unsigned tls_sign_usage(void) { return MBEDTLS_X509_KU_DIGITAL_SIGNATURE; }
unsigned tls_ca_usage(void) { return MBEDTLS_X509_KU_KEY_CERT_SIGN | MBEDTLS_X509_KU_CRL_SIGN; }
int tls_ecdsa_signature(void) { return MBEDTLS_PK_SIGALG_ECDSA; }
void tls_ssl_min_version(void *p, int version)
{ mbedtls_ssl_conf_min_tls_version(p, (mbedtls_ssl_protocol_version)version); }
void tls_ssl_max_version(void *p, int version)
{ mbedtls_ssl_conf_max_tls_version(p, (mbedtls_ssl_protocol_version)version); }
int tls_handshake_over(void *p) { return mbedtls_ssl_is_handshake_over(p); }
void *tls_crt_pk(void *p) { return &((mbedtls_x509_crt *)p)->pk; }
int tls_crt_ca(void *p) { return ((mbedtls_x509_crt *)p)->MBEDTLS_PRIVATE(ca_istrue); }
unsigned tls_crt_usage(void *p) { return ((mbedtls_x509_crt *)p)->MBEDTLS_PRIVATE(key_usage); }
int tls_crt_sig_md(void *p) { return ((mbedtls_x509_crt *)p)->MBEDTLS_PRIVATE(sig_md); }
int tls_crt_sig_pk(void *p) { return ((mbedtls_x509_crt *)p)->MBEDTLS_PRIVATE(sig_pk); }
int tls_crl_sig_md(void *p) { return ((mbedtls_x509_crl *)p)->MBEDTLS_PRIVATE(sig_md); }
int tls_crl_sig_pk(void *p) { return ((mbedtls_x509_crl *)p)->MBEDTLS_PRIVATE(sig_pk); }
const void *tls_crt_from(void *p) { return &((mbedtls_x509_crt *)p)->valid_from; }
const void *tls_crt_until(void *p) { return &((mbedtls_x509_crt *)p)->valid_to; }
const void *tls_crl_from(void *p) { return &((mbedtls_x509_crl *)p)->this_update; }
const void *tls_crl_until(void *p) { return &((mbedtls_x509_crl *)p)->next_update; }

/* Read-only views into parsed SDK-owned storage, valid while its owner lives. */
const unsigned char *tls_crt_raw(void *p, size_t *size)
{ mbedtls_x509_crt *v = p; *size = v->raw.len; return v->raw.p; }
const unsigned char *tls_crt_issuer(void *p, size_t *size)
{ mbedtls_x509_crt *v = p; *size = v->issuer_raw.len; return v->issuer_raw.p; }
const unsigned char *tls_crt_subject(void *p, size_t *size)
{ mbedtls_x509_crt *v = p; *size = v->subject_raw.len; return v->subject_raw.p; }
const unsigned char *tls_crt_eku(void *p, size_t *size)
{ mbedtls_x509_crt *v = p; *size = v->ext_key_usage.buf.len; return v->ext_key_usage.buf.p; }
const void *tls_crt_eku_next(void *p)
{ return ((mbedtls_x509_crt *)p)->ext_key_usage.next; }
const unsigned char *tls_crl_raw(void *p, size_t *size)
{ mbedtls_x509_crl *v = p; *size = v->raw.len; return v->raw.p; }
const unsigned char *tls_crl_issuer(void *p, size_t *size)
{ mbedtls_x509_crl *v = p; *size = v->issuer_raw.len; return v->issuer_raw.p; }
const unsigned char *tls_peer_raw(void *p, size_t *size)
{
  const mbedtls_x509_crt *peer = mbedtls_ssl_get_peer_cert(p);
  *size = peer == NULL ? 0 : peer->raw.len;
  return peer == NULL ? NULL : peer->raw.p;
}
