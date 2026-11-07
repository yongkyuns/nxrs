#include "clock.h"
#define MP_RAM_BEGIN 0x20000000u
#define MP_RAM_END 0x20082000u
#define MP_CONSOLE_PATH "/dev/ttyACM0"
#ifdef __NuttX__
int mp_console_init(void);
#else
void mp_console_init(void);
#endif
void mp_console_idle(void);
