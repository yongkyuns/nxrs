#ifndef NXRS_IDENTITY_TRUSTED_TIME_H
#define NXRS_IDENTITY_TRUSTED_TIME_H
#include <stdint.h>
#include <time.h>
/* Actual SDK callback ABI; Rust owns the call-scoped trusted observation. */
int64_t nxrs_di_trusted_utc(void);
time_t nxrs_di_sdk_time(time_t *output);
#endif
