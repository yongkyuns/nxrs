#include "clock.h"
void mp_clock_prepare(void) {es_clock_prepare();}
__attribute__((noinline)) uint32_t mp_clock32(void) {return es_clock_cycles();}
