/* X.509 enrollment and returned-certificate checks; no TLS or persistence. */
#ifndef NXRS_DEVICE_IDENTITY_MBEDTLS_CONFIG_H
#define NXRS_DEVICE_IDENTITY_MBEDTLS_CONFIG_H
#define MBEDTLS_X509_USE_C
#define MBEDTLS_X509_CREATE_C
#define MBEDTLS_X509_CSR_WRITE_C
#define MBEDTLS_X509_CRT_PARSE_C
#endif
