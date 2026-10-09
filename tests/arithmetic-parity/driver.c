/* SPDX-License-Identifier: MIT
 * Diagnostic driver for the fixed arithmetic parity ABI. */
#include "contract.h"

#include <inttypes.h>
#include <stdio.h>

#ifndef AQ_HOST
#  include <nuttx/config.h>
#  include <nuttx/irq.h>

#  ifndef CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ
#    error "arithmetic parity driver requires ESP32-S3"
#  endif
#  if CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ != 240
#    error "arithmetic parity driver requires a fixed 240 MHz CPU"
#  endif
#  ifdef CONFIG_SMP
#    error "arithmetic parity CCOUNT timing requires single-core NuttX"
#  endif
#else
/* Host timings are synthetic smoke-test counters, not measurements. */
typedef unsigned irqstate_t;
static irqstate_t enter_critical_section(void) { return 0; }
static void leave_critical_section(irqstate_t flags) { (void)flags; }
static volatile uint32_t aq_host_counter;
#endif

static struct aq_result c_output[AQ_SAMPLES];
static struct aq_result rust_output[AQ_SAMPLES];

static uint32_t read_cycles(void)
{
#ifdef AQ_HOST
  return aq_host_counter++;
#else
  uint32_t cycles;
  __asm__ volatile("rsr.ccount %0" : "=a"(cycles) : : "memory");
  return cycles;
#endif
}

static uint64_t ordered_float32(uint32_t bits)
{
  return (bits & UINT32_C(0x80000000))
           ? (uint64_t)(~bits)
           : (uint64_t)(bits | UINT32_C(0x80000000));
}

static uint64_t ordered_float64(uint64_t bits)
{
  return (bits & UINT64_C(0x8000000000000000))
           ? ~bits
           : (bits | UINT64_C(0x8000000000000000));
}

static int float32_nan(uint32_t bits)
{
  return (bits & UINT32_C(0x7f800000)) == UINT32_C(0x7f800000) &&
         (bits & UINT32_C(0x007fffff)) != 0;
}

static int float64_nan(uint64_t bits)
{
  return (bits & UINT64_C(0x7ff0000000000000)) ==
           UINT64_C(0x7ff0000000000000) &&
         (bits & UINT64_C(0x000fffffffffffff)) != 0;
}

/* Compare one result against its reference and return the finite ULP distance. */
static int result_matches(const struct aq_result *actual,
                          const struct aq_result *reference,
                          uint32_t float_bits, uint32_t ulp_limit,
                          uint32_t zero_sign_required,
                          uint64_t *ulp_out)
{
  uint64_t ulp = 0;
  int matches;

  if (float_bits == 0)
    {
      matches = actual->lo == reference->lo &&
                actual->hi == reference->hi &&
                actual->flag == reference->flag &&
                actual->reserved == reference->reserved;
      *ulp_out = 0;
      return matches;
    }

  if (float_bits == 32)
    {
      uint32_t a = (uint32_t)actual->lo;
      uint32_t r = (uint32_t)reference->lo;
      uint64_t ao = ordered_float32(a);
      uint64_t ro = ordered_float32(r);
      if (!zero_sign_required && !(r & UINT32_C(0x7fffffff)) &&
          !(a & UINT32_C(0x7fffffff)))
        a = r;
      ao = ordered_float32(a);
      if (!float32_nan(r) && !float32_nan(a) &&
          (a & UINT32_C(0x7f800000)) != UINT32_C(0x7f800000) &&
          (r & UINT32_C(0x7f800000)) != UINT32_C(0x7f800000))
        {
          ulp = ao >= ro ? ao - ro : ro - ao;
        }
      matches = (actual->lo >> 32) == (reference->lo >> 32) &&
                actual->hi == reference->hi &&
                actual->flag == reference->flag &&
                actual->reserved == reference->reserved;
      if (float32_nan(r))
        {
          matches = matches && float32_nan(a);
        }
      else if ((r & UINT32_C(0x7f800000)) == UINT32_C(0x7f800000))
        {
          matches = matches && a == r;
        }
      else
        {
          matches = matches &&
                    (a & UINT32_C(0x7f800000)) !=
                      UINT32_C(0x7f800000) &&
                    ((r & UINT32_C(0x7fffffff)) != 0 || a == r) &&
                    ulp <= ulp_limit;
        }
    }
  else if (float_bits == 64)
    {
      uint64_t a = actual->lo;
      uint64_t r = reference->lo;
      uint64_t ao = ordered_float64(a);
      uint64_t ro = ordered_float64(r);
      if (!zero_sign_required && !(r & UINT64_C(0x7fffffffffffffff)) &&
          !(a & UINT64_C(0x7fffffffffffffff)))
        a = r;
      ao = ordered_float64(a);
      if (!float64_nan(r) && !float64_nan(a) &&
          (a & UINT64_C(0x7ff0000000000000)) !=
            UINT64_C(0x7ff0000000000000) &&
          (r & UINT64_C(0x7ff0000000000000)) !=
            UINT64_C(0x7ff0000000000000))
        {
          ulp = ao >= ro ? ao - ro : ro - ao;
        }
      matches = actual->hi == reference->hi &&
                actual->flag == reference->flag &&
                actual->reserved == reference->reserved;
      if (float64_nan(r))
        {
          matches = matches && float64_nan(a);
        }
      else if ((r & UINT64_C(0x7ff0000000000000)) ==
               UINT64_C(0x7ff0000000000000))
        {
          matches = matches && a == r;
        }
      else
        {
          matches = matches &&
                    (a & UINT64_C(0x7ff0000000000000)) !=
                      UINT64_C(0x7ff0000000000000) &&
                    ((r & UINT64_C(0x7fffffffffffffff)) != 0 || a == r) &&
                    ulp <= ulp_limit;
        }
    }
  else
    {
      matches = 0;
    }

  *ulp_out = ulp;
  return matches;
}

static void report_failure(const char *backend, uint32_t id, uint32_t sample,
                           const struct aq_result *actual,
                           const struct aq_result *expected)
{
  printf("AQ_FAIL backend=%s id=%" PRIu32 " sample=%" PRIu32
         " actual_lo=0x%016" PRIx64 " actual_hi=0x%016" PRIx64
         " actual_flag=%" PRIu32 " expected_lo=0x%016" PRIx64
         " expected_hi=0x%016" PRIx64 " expected_flag=%" PRIu32 "\n",
         backend, id, sample, actual->lo, actual->hi, actual->flag,
         expected->lo, expected->hi, expected->flag);
}

static uint32_t time_call(aq_kernel kernel, const struct aq_case *test,
                          struct aq_result *output, int masked)
{
  uint32_t start;
  uint32_t end;
  irqstate_t flags = 0;

  if (masked)
    {
      flags = enter_critical_section();
    }
  start = read_cycles();
  kernel(test->inputs, output, test->count);
  end = read_cycles();
  if (masked)
    {
      leave_critical_section(flags);
    }
  return end - start;
}

int aq_driver_main(int argc, char **argv)
{
  uint64_t total_c_errors = 0;
  uint64_t total_rust_errors = 0;
  uint64_t total_samples = 0;
  uint32_t id;

  (void)argc;
  (void)argv;

  for (id = 0; id < aq_case_count; ++id)
    {
      const struct aq_case *test = &aq_cases[id];
      uint32_t c_errors = 0;
      uint32_t rust_errors = 0;
      uint32_t pair_diff = 0;
      uint64_t c_max_ulp = 0;
      uint64_t rust_max_ulp = 0;
      uint32_t c_reported = 0;
      uint32_t rust_reported = 0;
      uint32_t sample;

      if (test->count == 0 || test->count > AQ_SAMPLES || test->rust == NULL)
        return 2;

      if (test->rust != NULL)
        {
          test->rust(test->inputs, rust_output, test->count); /* warm */
          for (sample = 0; sample < test->count; ++sample)
            rust_output[sample] = (struct aq_result){UINT64_MAX, UINT64_MAX,
                                                    UINT32_MAX, UINT32_MAX};
          test->rust(test->inputs, rust_output, test->count);
        }
      if (test->c != NULL)
        {
          test->c(test->inputs, c_output, test->count); /* warm */
          for (sample = 0; sample < test->count; ++sample)
            c_output[sample] = (struct aq_result){UINT64_MAX, UINT64_MAX,
                                                 UINT32_MAX, UINT32_MAX};
          test->c(test->inputs, c_output, test->count);
        }

      for (sample = 0; sample < test->count && sample < AQ_SAMPLES; ++sample)
        {
          uint64_t ulp;
          int c_ok = 1;
          int rust_ok = 1;

          if (test->c != NULL)
            {
              c_ok = result_matches(&c_output[sample],
                                    &test->expected[sample], test->float_bits,
                                    test->ulp_limit, test->zero_sign_required, &ulp);
              if (ulp > c_max_ulp) c_max_ulp = ulp;
              if (!c_ok)
                {
                  ++c_errors;
                  if (c_reported < 4)
                    {
                      report_failure("c", id, sample, &c_output[sample],
                                     &test->expected[sample]);
                      ++c_reported;
                    }
                }
            }
          if (test->rust != NULL)
            {
              rust_ok = result_matches(&rust_output[sample],
                                       &test->expected[sample],
                                       test->float_bits, test->ulp_limit,
                                       test->zero_sign_required, &ulp);
              if (ulp > rust_max_ulp) rust_max_ulp = ulp;
              if (!rust_ok)
                {
                  ++rust_errors;
                  if (rust_reported < 4)
                    {
                      report_failure("rust", id, sample,
                                     &rust_output[sample],
                                     &test->expected[sample]);
                      ++rust_reported;
                    }
                }
            }
          if (test->c != NULL && test->rust != NULL)
            {
              /* Raw differences remain visible even within a libm ULP budget. */
              if (c_output[sample].lo != rust_output[sample].lo ||
                  c_output[sample].hi != rust_output[sample].hi ||
                  c_output[sample].flag != rust_output[sample].flag ||
                  c_output[sample].reserved != rust_output[sample].reserved)
                {
                  ++pair_diff;
                }
            }
        }

      total_c_errors += c_errors;
      total_rust_errors += rust_errors;
      total_samples += test->count;
      printf("AQ_CASE id=%" PRIu32 " count=%" PRIu32 " c_present=%u"
             " c_errors=%" PRIu32 " rust_errors=%" PRIu32
             " pair_diff=%" PRIu32 " c_max_ulp=%" PRIu64
             " rust_max_ulp=%" PRIu64 "\n",
             id, test->count, (unsigned)(test->c != NULL), c_errors, rust_errors,
             pair_diff, c_max_ulp, rust_max_ulp);

      for (uint32_t mode = 0; mode < 2; ++mode)
        {
          for (uint32_t repeat = 0; repeat < AQ_REPEATS; ++repeat)
            {
              int masked = mode == 0;
              uint32_t c_cycles = 0;
              uint32_t rust_cycles = 0;
              int rust_first = ((id + repeat) & 1u) != 0;

              if (rust_first)
                {
                  if (test->rust != NULL)
                    {
                      test->rust(test->inputs, rust_output, test->count);
                      rust_cycles = time_call(test->rust, test, rust_output,
                                              masked);
                    }
                  if (test->c != NULL)
                    {
                      test->c(test->inputs, c_output, test->count);
                      c_cycles = time_call(test->c, test, c_output, masked);
                    }
                }
              else
                {
                  if (test->c != NULL)
                    {
                      test->c(test->inputs, c_output, test->count);
                      c_cycles = time_call(test->c, test, c_output, masked);
                    }
                  if (test->rust != NULL)
                    {
                      test->rust(test->inputs, rust_output, test->count);
                      rust_cycles = time_call(test->rust, test, rust_output,
                                              masked);
                    }
                }
              printf("AQ_TIME id=%" PRIu32 " mode=%s repeat=%" PRIu32
                     " c_cycles=%" PRIu32 " rust_cycles=%" PRIu32 "\n",
                     id, masked ? "masked" : "normal", repeat,
                     c_cycles, rust_cycles);
            }
        }
    }

  printf("AQ_DONE cases=%" PRIu32 " samples=%" PRIu64
         " c_errors=%" PRIu64 " rust_errors=%" PRIu64 " repeats=%u\n",
         aq_case_count, total_samples, total_c_errors, total_rust_errors,
         AQ_REPEATS);
  return total_c_errors == 0 && total_rust_errors == 0 ? 0 : 1;
}
