/* Header translation only. Allocation, ownership and SDK calls stay in Rust. */
#include <stddef.h>
#include <stdint.h>
#include <mbedtls/rsa.h>
#include <mbedtls/md.h>
#include <mbedtls/sha256.h>
_Static_assert(sizeof(mbedtls_md_type_t) == sizeof(int), "SDK enum ABI");
size_t nxrs_admission_rsa_size(void) { return sizeof(mbedtls_rsa_context); }
size_t nxrs_admission_rsa_align(void) { return _Alignof(mbedtls_rsa_context); }
size_t nxrs_admission_sha_size(void) { return sizeof(mbedtls_sha256_context); }
size_t nxrs_admission_sha_align(void) { return _Alignof(mbedtls_sha256_context); }
int nxrs_admission_sha256_id(void) { return MBEDTLS_MD_SHA256; }

/* Explicit ELF cross-build witness; never a host-format section guess. */
#ifdef NXRS_ADMISSION_ABI_LAYOUT_WITNESS
const uint32_t nxrs_admission_abi_layout[6]
  __attribute__((section(".nxrs_admission_abi"), used)) = {
    sizeof(mbedtls_rsa_context), _Alignof(mbedtls_rsa_context),
    MBEDTLS_MD_SHA256, sizeof(void *),
    sizeof(mbedtls_sha256_context), _Alignof(mbedtls_sha256_context)
  };
#endif
