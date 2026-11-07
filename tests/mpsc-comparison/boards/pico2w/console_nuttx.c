#include <nuttx/config.h>
#include <sys/boardctl.h>
#include <stdint.h>
#include <stddef.h>
int mp_console_init(void) {
 void *handle=NULL;
 struct boardioc_usbdev_ctrl_s ctrl={.usbdev=BOARDIOC_USBDEV_CDCACM,.action=BOARDIOC_USBDEV_CONNECT,.instance=0,.handle=&handle};
 return boardctl(BOARDIOC_USBDEV_CONTROL,(uintptr_t)&ctrl);
}
