/* Actual target-header ABI witness; compiled separately, never linked into firmware. */
#include <stddef.h>
#include <stdint.h>
#include <bootutil/sign_key.h>

const uint32_t nxrs_key_layout[] = {
  sizeof(struct bootutil_key),
  offsetof(struct bootutil_key, key),
  offsetof(struct bootutil_key, len),
  sizeof(void *),
  sizeof(bootutil_key_cnt),
};
