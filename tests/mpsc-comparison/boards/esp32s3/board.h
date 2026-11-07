#include "clock.h"
#define MP_RAM_BEGIN 0x3fc88000u
#define MP_RAM_END 0x3fd00000u
#define MP_CONSOLE_PATH "/dev/console"
#ifdef __NuttX__
int mp_console_init(void);
#else
void mp_console_init(void);
#endif
void mp_console_idle(void);
