#include <zephyr/kernel.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/usb/usb_device.h>
#include <pico/bootrom.h>
void mp_console_init(void) {
 const struct device *uart=DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
 if(usb_enable(NULL))k_panic();
 uint32_t dtr=0;
 while(!dtr){uart_line_ctrl_get(uart,UART_LINE_CTRL_DTR,&dtr);k_sleep(K_MSEC(10));}
}
/* No recovery polling runs during measurement. Drain output at 1200 baud if
 * recovering an interrupted report, then the main thread reaches this hook. */
void mp_console_idle(void) {
 const struct device *uart=DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
 for(;;){uint32_t baud=0;uart_line_ctrl_get(uart,UART_LINE_CTRL_BAUD_RATE,&baud);if(baud==1200)reset_usb_boot(0,1);k_sleep(K_MSEC(10));}
}
