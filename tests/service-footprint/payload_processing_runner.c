/* Same-image paired CPU measurements: no queues, threads, heap or output in
 * the timed region. Buffers are initialized once and overwritten in place.
 */
#include "payload_processing.h"
#include <stdio.h>
#include <string.h>

extern uint32_t nxrs_cq_cycles(void);
extern uint32_t nxrs_cq_heap_used(void);
extern uint32_t nxrs_cq_stack_hwm(void);

typedef int (*encode_fn)(uint8_t *, size_t, uint32_t, const int16_t *);
typedef int (*parse_fn)(const uint8_t *, size_t, int16_t *, uint32_t *);
typedef void (*filter_fn)(const int16_t *, int32_t *, int32_t *);
struct kernels { encode_fn encode; parse_fn parse; filter_fn filter, filter_xyz; };
enum { FRAMES = 4096, BATCHES = 6 };

static void input(int16_t *samples, uint32_t seed)
{
  for (unsigned i = 0; i < NXRS_PAYLOAD_SAMPLES; ++i)
    samples[i] = (int16_t)((int32_t)((seed + i * 7919u) & 65535u) - 32768);
}

static int validate(const struct kernels *implementations)
{
  int16_t samples[96], decoded[96] = {0};
  uint8_t packets[2][200];
  int32_t states[2][3], outputs[2][96] = {{0}}, expected_state[3];
  for (uint32_t trial = 0; trial < 128; ++trial)
    {
      input(samples, trial * 65537u);
      samples[0] = -32768; samples[1] = 32767; samples[2] = -1;
      uint32_t seq = trial * 0x1020304u;
      for (unsigned language = 0; language < 2; ++language)
        {
          const struct kernels *k = &implementations[language];
          uint32_t parsed = ~seq;
          memset(packets[language], 0xa5, 200);
          if (k->encode(packets[language], 200, seq, samples) != 0 ||
              k->parse(packets[language], 200, decoded, &parsed) != 0 ||
              parsed != seq || memcmp(samples, decoded, sizeof samples) != 0)
            return -1;
          states[language][0] = -32768; states[language][1] = 32767;
          states[language][2] = 0;
          k->filter(samples, states[language], outputs[language]);
          /* Independent wider reference, not merely agreement between twins. */
          expected_state[0] = -32768; expected_state[1] = 32767; expected_state[2] = 0;
          for (unsigned i = 0; i < 96; ++i)
            {
              unsigned axis = i % 3;
              int64_t step = ((int64_t)samples[i] - expected_state[axis]) / 8;
              expected_state[axis] += (int32_t)step;
              if (outputs[language][i] != expected_state[axis]) return -1;
            }
          if (memcmp(states[language], expected_state, sizeof expected_state) != 0)
            return -1;
          states[language][0] = -32768; states[language][1] = 32767; states[language][2] = 0;
          k->filter_xyz(samples, states[language], outputs[language]);
          expected_state[0] = -32768; expected_state[1] = 32767; expected_state[2] = 0;
          for (unsigned i = 0; i < 96; ++i)
            {
              unsigned axis = i % 3;
              expected_state[axis] += (int32_t)(((int64_t)samples[i] - expected_state[axis]) / 8);
              if (outputs[language][i] != expected_state[axis]) return -1;
            }
          /* Invalid inputs must not write outputs. */
          for (unsigned field = 4; field < 8; ++field)
            {
              packets[language][field] ^= 1;
              memcpy(decoded, samples, sizeof samples); parsed = ~seq;
              if (k->parse(packets[language], 200, decoded, &parsed) != -1 ||
                  parsed != ~seq || memcmp(decoded, samples, sizeof samples) != 0)
                return -1;
              packets[language][field] ^= 1;
            }
          for (size_t length = 199; length <= 201; length += 2)
            if (k->encode(packets[language], length, seq, samples) != -1 ||
                k->parse(packets[language], length, decoded, &parsed) != -1)
              return -1;
          if (k->encode(NULL, 200, seq, samples) != -1 ||
              k->parse(NULL, 200, decoded, &parsed) != -1) return -1;
        }
      if (memcmp(packets[0], packets[1], 200) != 0 ||
          memcmp(outputs[0], outputs[1], sizeof outputs[0]) != 0) return -1;
      for (unsigned i = 0; i < 4; ++i)
        if (packets[0][i] != (uint8_t)(seq >> (i * 8))) return -1;
      for (unsigned i = 0; i < 96; ++i)
        if (packets[0][8 + i * 2] != (uint8_t)(uint16_t)samples[i] ||
            packets[0][9 + i * 2] != (uint8_t)((uint16_t)samples[i] >> 8)) return -1;
    }
  return 0;
}

struct result { uint32_t cycles, digest; int status; };

static struct result measure(const struct kernels *implementations,
                             unsigned kind, unsigned language, unsigned frames)
{
  const struct kernels *k = &implementations[language];
  /* Rust's typed mutable references require initialized storage, even though
   * these kernels overwrite it. Initialize once before the timer, equally for
   * both languages; never manufacture a reference to uninitialized integers. */
  int16_t corpus[8][96], decoded[96] = {0};
  uint8_t packets[8][200], packet[200] = {0};
  int32_t state[3] = {-1234, 2345, -3456}, output[96] = {0};
  uint32_t seq = 0;
  for (unsigned frame = 0; frame < 8; ++frame)
    {
      input(corpus[frame], 12345 + frame * 977u);
      if (nxrs_c_encode(packets[frame], 200, frame, corpus[frame]) != 0)
        return (struct result){0, 0, -1};
    }
  int status = 0;
  uint32_t started = nxrs_cq_cycles();
  for (unsigned i = 0; i < frames; ++i)
    {
      /* Input changes at runtime; separate C/Rust compilation and external
       * noinline entry points prevent constant folding of the kernels. */
      if (kind == 0)
        {
          status |= k->encode(packet, 200, i, corpus[i % 8]);
        }
      else if (kind == 1)
        {
          status |= k->parse(packets[i % 8], 200, decoded, &seq);
        }
      else
        {
          if (kind == 2) k->filter(corpus[i % 8], state, output);
          else k->filter_xyz(corpus[i % 8], state, output);
        }
    }
  uint32_t elapsed = nxrs_cq_cycles() - started;
  uint32_t digest = 0;
  /* Digest and validation are intentionally outside the timed region. */
  if (kind == 0)
    {
      if (nxrs_c_parse(packet, 200, decoded, &seq) != 0 ||
          seq != frames - 1 || memcmp(decoded, corpus[(frames - 1) % 8], sizeof decoded) != 0) status = -1;
      for (unsigned i = 0; i < 200; ++i) digest += packet[i];
    }
  else if (kind == 1)
    {
      if (seq != (frames - 1) % 8 ||
          memcmp(decoded, corpus[(frames - 1) % 8], sizeof decoded) != 0) status = -1;
      for (unsigned i = 0; i < 96; ++i) digest += (uint16_t)decoded[i];
      digest += seq;
    }
  else
    {
      int32_t reference[3] = {-1234, 2345, -3456};
      for (unsigned frame = 0; frame < frames; ++frame)
        for (unsigned i = 0; i < 96; ++i)
          {
            unsigned axis = i % 3;
            reference[axis] += (int32_t)(((int64_t)corpus[frame % 8][i] - reference[axis]) / 8);
            if (frame == frames - 1 && reference[axis] != output[i]) status = -1;
          }
      for (unsigned i = 0; i < 96; ++i) digest += (uint32_t)output[i];
      for (unsigned axis = 0; axis < 3; ++axis)
        if (state[axis] != reference[axis]) status = -1;
    }
  return (struct result){elapsed, digest, status};
}

int nxrs_payload_run(encode_fn encode, parse_fn parse, filter_fn filter, filter_fn filter_xyz)
{
  const struct kernels implementations[2] = {
    {nxrs_c_encode, nxrs_c_parse, nxrs_c_filter, nxrs_c_filter_xyz},
    {encode, parse, filter, filter_xyz}
  };
  static const char *names[] = {"encode", "parse", "filter", "filter-xyz"};
  if (validate(implementations) != 0) { puts("PAYLOAD_FAIL validation=1"); return 1; }
  uint32_t before = nxrs_cq_heap_used();
  for (unsigned kind = 0; kind < 4; ++kind)
    {
      for (unsigned language = 0; language < 2; ++language)
        if (measure(implementations, kind, language, 128).status != 0) return 1;
      for (unsigned batch = 0; batch < BATCHES; ++batch)
        {
          struct result result[2];
          unsigned first = batch % 2;
          result[first] = measure(implementations, kind, first, FRAMES);
          result[1 - first] = measure(implementations, kind, 1 - first, FRAMES);
          if (result[0].status != 0 || result[1].status != 0 ||
              result[0].digest != result[1].digest)
            { puts("PAYLOAD_FAIL timed_output=1"); return 1; }
          printf("PAYLOAD_CASE case=%s batch=%u frames=%u c_cycles=%lu rust_cycles=%lu digest=%lu first=%u\n",
                 names[kind], batch, FRAMES, (unsigned long)result[0].cycles,
                 (unsigned long)result[1].cycles, (unsigned long)result[0].digest, first);
        }
    }
  uint32_t after = nxrs_cq_heap_used();
  printf("PAYLOAD_PASS cases=4 batches=%u frames=%u bytes=200 samples=96 heap_before=%lu heap_after=%lu stack_hwm=%lu\n",
         BATCHES, FRAMES, (unsigned long)before, (unsigned long)after,
         (unsigned long)nxrs_cq_stack_hwm());
  return before == after ? 0 : 1;
}
