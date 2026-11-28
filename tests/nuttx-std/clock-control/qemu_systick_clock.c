/* Explicit qualification control for QEMU's MPS2-AN521 clock mismatch.
 *
 * At NuttX 4268617f28ad0ed7e900a081ea1b33c9347458b3, the AN521 board.h
 * specifies MPS_SYSTICK_CLOCK=32 MHz. mps_timer.c passes that frequency to
 * systick_initialize(), whose lower half converts counts using this value.
 * QEMU v10.0.13 instead supplies 20 MHz to this machine's SYSCLK and APB:
 * https://github.com/qemu/qemu/blob/v10.0.13/hw/arm/mps2-tz.c
 * Its MAINCLK reaches CPU cpuclk through armsse.c and then SysTick cpuclk
 * through armv7m.c; the SSE subsystem has no connected SysTick refclk.
 * https://github.com/qemu/qemu/blob/v10.0.13/hw/arm/armsse.c
 * https://github.com/qemu/qemu/blob/v10.0.13/hw/arm/armv7m.c
 * https://github.com/qemu/qemu/blob/v10.0.13/hw/timer/armv7m_systick.c
 *
 * Link this file ONLY for an explicitly labelled corrected control, with
 * --wrap=systick_initialize. The unmodified image remains a separate case.
 * up_timer_set_lowerhalf() programs the timeout after this constructor and
 * before starting the timer, replacing mps_timer.c's initial reload value.
 * The independent APB reference, clock APIs, and adjustment algorithm remain
 * unchanged. This is not a production BSP change or hardware qualification.
 */
#include <nuttx/config.h>
#include <arch/board/board.h>
#include <assert.h>
#include <stdbool.h>

/* The normal target-helper build includes the configured NuttX root. Use its
 * actual private declaration rather than copying the constructor's ABI.
 */
#include "arch/arm/src/armv8-m/systick.h"

#if !defined(CONFIG_ARCH_BOARD_MPS2_AN521) || \
    !defined(CONFIG_ARMV8M_SYSTICK) || !defined(CONFIG_TIMER_ARCH)
#  error "This clock control is specific to MPS2-AN521 with ARMv8-M SysTick"
#endif

_Static_assert(MPS_SYSTICK_CLOCK == 32000000u,
               "Reassess the explicit clock control when the BSP changes");

extern __typeof__(systick_initialize) __real_systick_initialize;

struct timer_lowerhalf_s *__wrap_systick_initialize(bool coreclk,
                                                   unsigned int freq,
                                                   int minor)
{
  ASSERT(coreclk && freq == MPS_SYSTICK_CLOCK && minor == -1);
  return __real_systick_initialize(coreclk, 20000000u, minor);
}

_Static_assert(__builtin_types_compatible_p(
                 __typeof__(&__wrap_systick_initialize),
                 __typeof__(&systick_initialize)),
               "The wrapper must match the selected NuttX declaration");
