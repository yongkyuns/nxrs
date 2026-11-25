/* SPDX-License-Identifier: MIT
 * Unit witness for the complete ECDSA identifier parser. The real host-key
 * dispatcher normally rejects unrecognized outer key names before this helper;
 * direct malformed-helper coverage is not a claim of remote exploitability.
 * Curve decoding is an observation stub, not cryptographic qualification.
 */

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TRACE(x)

typedef struct
{
  const char *strings[2];
  unsigned int next_string;
} buffer;

typedef struct
{
  unsigned int marker;
} ecc_key;

struct dropbear_ecc_curve
{
  const char *name;
};

static struct dropbear_ecc_curve p256 = {"nistp256"};
static struct dropbear_ecc_curve *dropbear_ecc_curves[] = {&p256, NULL};
static ecc_key decoded = {256};
static unsigned int allocations;
static unsigned int releases;
static unsigned int decode_calls;

/* Model buf_getstring's allocation contract, including its terminating NUL.
 * Exact short allocations make an over-read observable to AddressSanitizer.
 */

static char *buf_getstring(buffer *buf, unsigned int *length)
{
  const char *source;
  char *result;

  assert(buf->next_string < 2);
  source = buf->strings[buf->next_string++];
  *length = (unsigned int)strlen(source);
  result = malloc(*length + 1);
  assert(result != NULL);
  memcpy(result, source, *length + 1);
  allocations++;
  return result;
}

static void m_free(void *pointer)
{
  assert(pointer != NULL);
  releases++;
  free(pointer);
}

static buffer *buf_getstringbuf(buffer *buf)
{
  buffer *result;

  assert(buf->next_string == 2);
  result = calloc(1, sizeof(*result));
  assert(result != NULL);
  allocations++;
  return result;
}

static ecc_key *buf_get_ecc_raw_pubkey(buffer *buf,
                                    const struct dropbear_ecc_curve *curve)
{
  assert(buf != NULL && curve == &p256);
  decode_calls++;
  return &decoded;
}

static void buf_free(buffer *buf)
{
  m_free(buf);
}

/* Pinned upstream passes unsigned-char strings to strcmp in older lines. Keep
 * the complete definition verbatim and suppress only that existing warning.
 */

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wpointer-sign"
#include "ecdsa-identifier-function.inc"
#pragma GCC diagnostic pop

static void parse(const char *key_name, const char *curve_name, int valid)
{
  buffer input = {{key_name, curve_name}, 0};
  unsigned int previous_decodes = decode_calls;
  ecc_key *result = buf_get_ecdsa_pub_key(&input);

  assert(result == (valid ? &decoded : NULL));
  assert(decode_calls == previous_decodes + (unsigned int)valid);
  assert(allocations == releases);
}

int main(void)
{
  char short_curve[9];
  char key_name[32];
  size_t length;

  /* Matching outer/inner short identifiers exercise the helper's old fixed-
   * length memcmp directly. The fixed helper rejects all of them without
   * reading beyond buf_getstring's allocation or trying to decode a point.
   */

  for (length = 0; length < strlen(p256.name); length++)
    {
      memcpy(short_curve, p256.name, length);
      short_curve[length] = '\0';
      assert(snprintf(key_name, sizeof(key_name), "ecdsa-sha2-%s", short_curve)
             > 0);
      parse(key_name, short_curve, 0);
    }

  parse("ecdsa-sha2-nistp256", "nistp256", 1);
  parse("sk-ecdsa-sha2-nistp256@openssh.com", "nistp256", 1);
  parse("ecdsa-sha2-nistp256", "nistp255", 0);
  parse("ecdsa-sha2-nistp256", "nistp25", 0);
  parse("ecdsa-sha2-nistp256x", "nistp256x", 0);
  parse("sk-ecdsa-sha2-nistp256@openssh.com", "nistp25", 0);
  puts("PASS short and mismatched ECDSA identifiers refuse before point decoding");
  return 0;
}
