/* SPDX-License-Identifier: MIT
 * Diagnostic ABI shared by independently compiled C and Rust kernels. */
#ifndef NXRS_ARITHMETIC_PARITY_CONTRACT_H
#define NXRS_ARITHMETIC_PARITY_CONTRACT_H
#include <stdint.h>
#include <stddef.h>

struct aq_input { uint64_t a, b, c, ah, bh, ch; };
struct aq_result { uint64_t lo, hi; uint32_t flag, reserved; };
typedef void (*aq_kernel)(const struct aq_input *, struct aq_result *, uint32_t);
struct aq_case {
  const char *name;
  aq_kernel c, rust;
  const struct aq_input *inputs;
  const struct aq_result *expected;
  uint32_t count, float_bits, ulp_limit, zero_sign_required;
};

#define AQ_SAMPLES 64u
#define AQ_REPEATS 5u
/* Inputs and expected results live in flash; output buffers are bounded RAM. */
extern const struct aq_case aq_cases[];
extern const uint32_t aq_case_count;
int aq_driver_main(int argc, char **argv);
#endif
