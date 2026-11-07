#include <zephyr/kernel.h>
void mp_console_init(void) {}
void mp_console_idle(void) {for(;;)k_sleep(K_FOREVER);}
