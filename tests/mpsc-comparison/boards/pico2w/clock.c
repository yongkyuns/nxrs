#include "clock.h"
void mp_clock_prepare(void) {}
__attribute__((noinline)) uint32_t mp_clock32(void) {return *(volatile uint32_t *)0x400b0028u;}
