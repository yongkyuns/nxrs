/* Scripted MMIO for actual NuttX timer source, not a clock implementation.
 * The MPS2 profile uses a 32 MHz counter and a 1 ms scheduler tick. These
 * controls cover one unserviced wrap; a pending bit cannot count lost wraps.
 */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

typedef unsigned irqstate_t;
typedef bool (*tccb_t)(uint32_t *, void *);
struct timer_ops_s;
struct timer_lowerhalf_s { const struct timer_ops_s *ops; };
struct timer_status_s { uint32_t flags, timeout, timeleft; };

/* Symbolic register identities: no host access to ARM addresses. */
enum { NVIC_SYSTICK_CTRL, NVIC_SYSTICK_RELOAD, NVIC_SYSTICK_CURRENT, NVIC_INTCTRL };
#define NVIC_SYSTICK_CTRL_ENABLE 1u
#define NVIC_INTCTRL_PENDSTSET (1u << 26)
#define TCFLAGS_ACTIVE 1u
#define TCFLAGS_HANDLER 2u
#define USEC_PER_SEC 1000000u
#define TICK2USEC(ticks) ((uint64_t)(ticks) * 1000u)
#define FAR
#define TIMER_GETSTATUS(lower, status) systick_getstatus(lower, status)

static irqstate_t up_irq_save(void);
static void up_irq_restore(irqstate_t flags);
static uint32_t getreg32(uintptr_t address);
#include "systick.inc"

static struct systick_lowerhalf_s lower;
static unsigned irq_depth, current_reads, pending_reads, status_calls;
static uint32_t first_current, post_pending_current, callback_interval;
static bool pending, running, advance_timebase;

static irqstate_t up_irq_save(void)
{
  irqstate_t previous = irq_depth;
  irq_depth++;
  status_calls++;
  assert(status_calls <= 2);
  return previous;
}

static void up_irq_restore(irqstate_t flags)
{
  assert(irq_depth == flags + 1);
  irq_depth = flags;
  if (advance_timebase)
    {
      /* Deliver one ISR after the lower-half sample. The real upper-half
       * current_usec must discard that sample and retry the changed base.
       */
      assert(irq_depth == 0);
      g_timer.timebase++;
      first_current = post_pending_current = 31968;
      current_reads = pending_reads = 0;
      pending = advance_timebase = false;
    }
}

static uint32_t getreg32(uintptr_t address)
{
  assert(irq_depth == 1);
  switch (address)
    {
      case NVIC_SYSTICK_CTRL:
        return running ? NVIC_SYSTICK_CTRL_ENABLE : 0;
      case NVIC_SYSTICK_RELOAD:
        return 31999; /* 32000 counter cycles per 1 ms. */
      case NVIC_SYSTICK_CURRENT:
        assert(++current_reads <= 2);
        return pending_reads ? post_pending_current : first_current;
      case NVIC_INTCTRL:
        assert(current_reads == 1 && ++pending_reads == 1);
        return pending ? NVIC_INTCTRL_PENDSTSET : 0;
      default:
        assert(false);
        return 0;
    }
}

static bool callback(uint32_t *interval, void *argument)
{
  (void)interval;
  (void)argument;
  assert(false); /* Interrupt delivery is explicit in each script. */
  return false;
}

static uint64_t read_clock(unsigned ticks, uint32_t before, bool is_pending,
                           uint32_t after, bool in_callback, bool retry,
                           bool active)
{
  assert(irq_depth == 0);
  current_reads = pending_reads = status_calls = 0;
  first_current = before;
  post_pending_current = after;
  pending = is_pending;
  running = active;
  advance_timebase = retry;
  lower.freq = 32000000;
  lower.callback = active ? callback : NULL;
  lower.next_interval = in_callback ? &callback_interval : NULL;
  g_timer.lower = (struct timer_lowerhalf_s *)&lower;
  g_timer.timebase = ticks;
  uint64_t elapsed = current_usec();
  assert(irq_depth == 0 && !advance_timebase);
  assert(status_calls == (retry ? 2u : 1u));
  assert(pending_reads == (in_callback ? 0u : 1u));
  return elapsed;
}

int main(int argc, char **argv)
{
  assert(argc == 2);
  uint64_t actual[4], expected[4];
  unsigned count = 1;
  const char *name = argv[1];
  if (strcmp(name, "normal") == 0 || strcmp(name, "callback") == 0)
    {
      bool inside = strcmp(name, "callback") == 0;
      actual[0] = read_clock(12, 16000, inside, 16000, inside, false, true);
      actual[1] = read_clock(12, 8000, inside, 8000, inside, false, true);
      expected[0] = 12500;
      expected[1] = 12750;
      count = 2;
    }
  else if (strcmp(name, "rollover") == 0)
    {
      /* First sample precedes rollover; PENDSTSET observes it afterwards.
       * A reread observes the first microsecond of the following interval.
       */
      actual[0] = read_clock(12, 32, true, 31968, false, false, true);
      actual[1] = read_clock(13, 31936, false, 31936, false, false, true);
      expected[0] = 13001;
      expected[1] = 13002;
      count = 2;
    }
  else if (strcmp(name, "fractional") == 0)
    {
      /* A nonzero 31-cycle remainder is less than one microsecond. It is
       * neither a reset counter nor the exact zero at a pending rollover.
       */
      actual[0] = read_clock(12, 32, false, 32, false, false, true);
      actual[1] = read_clock(12, 31, false, 31, false, false, true);
      actual[2] = read_clock(12, 0, true, 0, false, false, true);
      actual[3] = read_clock(13, 31968, false, 31968, false, false, true);
      expected[0] = 12999;
      expected[1] = expected[2] = 13000;
      expected[3] = 13001;
      count = 4;
    }
  else if (strcmp(name, "pending-fractional") == 0)
    {
      actual[0] = read_clock(12, 31, true, 31, false, false, true);
      expected[0] = 14000;
    }
  else if (strcmp(name, "pending") == 0)
    {
      actual[0] = read_clock(12, 16000, true, 16000, false, false, true);
      expected[0] = 13500;
    }
  else if (strcmp(name, "pending-zero") == 0)
    {
      actual[0] = read_clock(12, 0, true, 0, false, false, true);
      expected[0] = 13000;
    }
  else if (strcmp(name, "reset-zero") == 0 || strcmp(name, "stopped") == 0)
    {
      bool active = strcmp(name, "reset-zero") == 0;
      actual[0] = read_clock(12, 0, false, 0, false, false, active);
      expected[0] = 12000;
    }
  else if (strcmp(name, "timebase-retry") == 0)
    {
      actual[0] = read_clock(12, 16000, false, 16000, false, true, true);
      expected[0] = 13001;
    }
  else
    {
      assert(false);
    }

  bool valid = true;
  for (unsigned i = 0; i < count; i++)
    {
      printf("%s%llu", i ? " " : "", (unsigned long long)actual[i]);
      valid &= actual[i] == expected[i];
      valid &= i == 0 || actual[i] >= actual[i - 1];
    }
  putchar('\n');
  if (!valid)
    {
      fputs("clock contract failed\n", stderr);
      return 1;
    }
  return 0;
}
