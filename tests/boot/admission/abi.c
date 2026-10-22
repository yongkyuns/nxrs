/* Header translation only. Allocation, ownership and SDK calls stay in Rust. */
#include <stddef.h>
#include <mbedtls/rsa.h>
#include <mbedtls/md.h>
_Static_assert(sizeof(mbedtls_md_type_t) == sizeof(int), "SDK enum ABI");
size_t nxrs_admission_rsa_size(void) { return sizeof(mbedtls_rsa_context); }
size_t nxrs_admission_rsa_align(void) { return _Alignof(mbedtls_rsa_context); }
int nxrs_admission_sha256_id(void) { return MBEDTLS_MD_SHA256; }
