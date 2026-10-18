/* SPDX-License-Identifier: MIT */
/* Compile the actual adapter callbacks with distinct allocator boundaries. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>

static unsigned kernel_frees;
static unsigned wrong_heap_frees;
static bool external_ram;

static void *kmm_malloc(size_t size)
{
  return malloc(size);
}

static void kernel_free(void *pointer)
{
  if (pointer == NULL)
    {
      return;
    }
  kernel_frees++;
  free(pointer);
}

#ifdef SEPARATE_HEAPS
#  define kmm_free kernel_free
#else
/* Match nuttx/kmalloc.h: this form is callable but not addressable. */
#  define kmm_free(pointer) kernel_free(pointer)
#endif

static void user_free(void *pointer)
{
#ifdef SEPARATE_HEAPS
  wrong_heap_frees += pointer != NULL;
#endif
  kmm_free(pointer);
}

static bool esp32s3_ptr_extram(void *pointer)
{
  assert(pointer != NULL);
  return external_ram;
}

#define free user_free
#define DEBUGASSERT assert
#define PERIPH_BT_MODULE 0
#define ESP_BT_CONTROLLER_STATUS_IDLE 0
#define periph_module_disable(module) ((void)(module))
#define btdm_low_power_mode_deinit() ((void)0)
#define esp_bt_power_domain_off() ((void)0)
#define esp_phy_modem_deinit() ((void)0)
static void *g_osi_funcs_p;
static int g_btdm_controller_status = 1;
#include "esp32s3-ble-allocator-functions.h"

int main(void)
{
#ifdef EXPECT_BASELINE_BUGS
  assert(controller_free == user_free);
#else
  assert(controller_free == free_wrapper && controller_free != user_free);
#endif
  controller_free(NULL);
  controller_free(malloc_wrapper(32));
  controller_free(malloc_internal_wrapper(16));
  external_ram = true;
  assert(malloc_internal_wrapper(16) == NULL);
  g_osi_funcs_p = kmm_malloc(64);
  bt_controller_deinit_internal();
  assert(g_osi_funcs_p == NULL);
  assert(g_btdm_controller_status == ESP_BT_CONTROLLER_STATUS_IDLE);
  assert(kernel_frees == 4);
#if defined(EXPECT_BASELINE_BUGS) && defined(SEPARATE_HEAPS)
  assert(wrong_heap_frees == 3);
#else
  assert(wrong_heap_frees == 0);
#endif
  return 0;
}
