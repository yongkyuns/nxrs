/* Header-defined Linux entropy flag and scalar ABI only. */
#include <stdint.h>
#include <sys/types.h>
#include <sys/random.h>
_Static_assert(sizeof(unsigned int) == sizeof(uint32_t), "getrandom flag ABI");
_Static_assert(sizeof(ssize_t) == sizeof(intptr_t), "getrandom result ABI");
uint32_t tls_nonblocking_entropy_flag(void) { return GRND_NONBLOCK; }
