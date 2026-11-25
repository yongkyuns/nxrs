#include <stddef.h>
#include <lvgl/lvgl.h>

#if LVGL_VERSION_MAJOR != 9 || LVGL_VERSION_MINOR != 2 || LVGL_VERSION_PATCH != 2
#  error "The Rust declarations are for LVGL 9.2.2"
#endif

#if LV_USE_OS != LV_OS_NONE || LV_DRAW_SW_DRAW_UNIT_CNT != 1 || LV_USE_NUTTX
#  error "The Rust adapter requires single-threaded LVGL with no native display driver"
#endif

#if LV_COLOR_DEPTH != 16 || LV_DRAW_BUF_STRIDE_ALIGN != 1 || LV_DRAW_BUF_ALIGN != 4
#  error "The Rust flush requires packed RGB565 with a 4-byte aligned buffer"
#endif

#if defined(LV_COLOR_16_SWAP) && LV_COLOR_16_SWAP
#  error "The display HAL accepts native-endian RGB565 words"
#endif

_Static_assert(sizeof(lv_color_format_t) == sizeof(unsigned int) &&
               LV_COLOR_FORMAT_RGB565 == 0x12, "RGB565 ABI");
_Static_assert(sizeof(lv_display_render_mode_t) == sizeof(unsigned int) &&
               LV_DISPLAY_RENDER_MODE_PARTIAL == 0, "partial mode ABI");
_Static_assert(sizeof(lv_area_t) == 16 && _Alignof(lv_area_t) == _Alignof(int32_t) &&
               offsetof(lv_area_t, x1) == 0 && offsetof(lv_area_t, y1) == 4 &&
               offsetof(lv_area_t, x2) == 8 && offsetof(lv_area_t, y2) == 12,
               "flush coordinates ABI");
_Static_assert(__builtin_types_compatible_p(lv_display_flush_cb_t,
               void (*)(lv_display_t *, const lv_area_t *, uint8_t *)), "flush callback ABI");
