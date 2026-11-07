#include <stdint.h>
/* Identical RP2350 TIMER0 free-running wall counter, 1 MHz on both kernels. */
static inline uint64_t mp_wall_us(void) {
 volatile uint32_t *hi=(void *)0x400b0024u,*lo=(void *)0x400b0028u;
 uint32_t a,b,l;do {a=*hi;l=*lo;b=*hi;} while(a!=b);
 return ((uint64_t)a<<32)|l;
}

static inline uint64_t mp_wall_ns(void) {return mp_wall_us()*1000u;}
