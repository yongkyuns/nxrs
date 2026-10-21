/* Exercise extracted esp_irq.c functions with kernel/SDK ownership stubs. */

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CONFIG_SMP_NCPUS 2
#define NR_IRQS 64
#define ETS_INTERNAL_INTR_SOURCE_OFF 16
#define ESP_SOURCE2IRQ(source) ((source) + 32)
#define IRQ_UNMAPPED NULL
#define IRAM_ATTR
#define OK 0
#define ESP_OK 0
#define UNUSED(value) ((void)(value))
#define irqinfo(...) ((void)0)
#define irqerr(...) ((void)0)
#define irqwarn(...) ((void)0)

typedef int esp_err_t;
typedef int (*xcpt_t)(int, void *, void *);
struct sdk_handle
{
  bool live;
  int cpu;
  void (*handler)(void *);
  void *arg;
};
typedef struct sdk_handle *intr_handle_t;

/* TYPES */
/* MAP */

static struct sdk_handle sdk_handles[8];
static struct { void *pointer; size_t size; } allocations[8];
static size_t live_bytes;
static unsigned allocation_calls, release_calls, sdk_allocations, sdk_detaches;
static int current_cpu, fail_kmm, fail_alloc, fail_detach;

static int this_cpu(void) { return current_cpu; }

static void *kmm_calloc(size_t count, size_t size)
{
  if (fail_kmm) { fail_kmm = 0; return NULL; }
  for (unsigned i = 0; i < 8; i++)
    if (!allocations[i].pointer)
      {
        void *pointer = calloc(count, size);
        assert(pointer);
        allocations[i].pointer = pointer;
        allocations[i].size = count * size;
        allocation_calls++;
        live_bytes += count * size;
        return pointer;
      }
  abort();
}

static void kmm_free(void *pointer)
{
  if (!pointer) return;
  for (unsigned i = 0; i < 8; i++)
    assert(!sdk_handles[i].live || sdk_handles[i].arg != pointer);
  for (unsigned i = 0; i < 8; i++)
    if (allocations[i].pointer == pointer)
      {
        live_bytes -= allocations[i].size;
        allocations[i].pointer = NULL;
        release_calls++;
        free(pointer);
        return;
      }
  assert(!"free of an unowned adapter");
}

static esp_err_t esp_intr_alloc_intrstatus(int source, int flags,
                                         uint32_t reg, uint32_t mask,
                                         void (*handler)(void *), void *arg,
                                         intr_handle_t *result)
{
  (void)source;
  assert(flags == 9 && reg == 0x1234 && mask == 0x40);
  sdk_allocations++;
  if (fail_alloc) { fail_alloc = 0; return -ENOMEM; }
  for (unsigned i = 0; i < 8; i++)
    if (!sdk_handles[i].live)
      {
        sdk_handles[i] = (struct sdk_handle){true, this_cpu(), handler, arg};
        *result = &sdk_handles[i];
        return ESP_OK;
      }
  abort();
}

static esp_err_t esp_intr_free(intr_handle_t handle)
{
  assert(handle && handle->live);
  sdk_detaches++;
  if (fail_detach) { fail_detach = 0; return -EIO; }
  handle->live = false;
  handle->arg = NULL;
  handle->handler = NULL;
  return ESP_OK;
}

static int esp_intr_get_intno(intr_handle_t handle)
{ assert(handle && handle->live); return 7; }
static int esp_intr_get_cpu(intr_handle_t handle)
{ assert(handle && handle->live); return handle->cpu; }

/* FUNCTIONS */

static int callback(int irq, void *context, void *arg)
{
  (void)irq;
  (void)context;
  (*(int *)arg)++;
  return OK;
}

static int setup(int source, int *calls)
{
  return esp_setup_irq_with_flags_intrstatus(source, 9, 0x1234, 0x40,
                                             callback, calls);
}

static void invoke(intr_handle_t handle)
{
  assert(handle && handle->live && handle->handler);
  handle->handler(handle->arg);
}

static void assert_empty(void)
{
  assert(live_bytes == 0);
  assert(allocation_calls == release_calls);
  for (unsigned i = 0; i < 8; i++)
    assert(!sdk_handles[i].live && !allocations[i].pointer);
  for (int cpu = 0; cpu < CONFIG_SMP_NCPUS; cpu++)
    for (int irq = 0; irq < NR_IRQS; irq++)
      assert(esp_get_handle(cpu, irq) == IRQ_UNMAPPED);
}

static void test_repeated_setup_teardown(void)
{
  int calls = 0;
  for (int cycle = 0; cycle < 200; cycle++)
    {
      assert(setup(1, &calls) == 7);
      intr_handle_t handle = esp_get_handle(0, ESP_SOURCE2IRQ(1));
      assert(esp_get_irq(0, handle) == ESP_SOURCE2IRQ(1));
      invoke(handle);
      assert(calls == cycle + 1);
      esp_teardown_irq(1, 7);
      assert_empty();
    }
}

static void test_failed_detach_retains_callback_for_retry(void)
{
  int calls = 0;
  assert(setup(1, &calls) == 7);
  intr_handle_t handle = esp_get_handle(0, ESP_SOURCE2IRQ(1));
  void *adapter = handle->arg;
  size_t bytes = live_bytes;
  unsigned frees = release_calls;
  fail_detach = 1;
  esp_teardown_irq(1, 7);
  assert(esp_get_handle(0, ESP_SOURCE2IRQ(1)) == handle);
  assert(handle->live && handle->arg == adapter);
  assert(live_bytes == bytes && release_calls == frees);
  invoke(handle);
  assert(calls == 1);
  esp_teardown_irq(1, 7);
  assert_empty();
  unsigned detaches = sdk_detaches;
  esp_teardown_irq(1, 7);
  assert(sdk_detaches == detaches);
}

static void test_allocation_failures(void)
{
  int calls = 0;
  unsigned attempts = sdk_allocations;
  unsigned allocations_before = allocation_calls;
  fail_kmm = 1;
  assert(setup(1, &calls) < 0);
  assert(sdk_allocations == attempts);
  assert(allocation_calls == allocations_before);
  assert_empty();

  unsigned detaches = sdk_detaches;
  fail_alloc = 1;
  assert(setup(1, &calls) < 0);
  assert(sdk_detaches == detaches && calls == 0);
  assert_empty();
}

static void test_duplicate_irq_preserves_existing_handler(void)
{
  int first_calls = 0, duplicate_calls = 0;
  assert(setup(1, &first_calls) == 7);
  intr_handle_t first = esp_get_handle(0, ESP_SOURCE2IRQ(1));
  void *adapter = first->arg;
  size_t bytes = live_bytes;
  unsigned detaches = sdk_detaches;
  assert(setup(1, &duplicate_calls) < 0);
  assert(esp_get_handle(0, ESP_SOURCE2IRQ(1)) == first);
  assert(first->arg == adapter && live_bytes == bytes);
  assert(sdk_detaches == detaches + 1);
  invoke(first);
  assert(first_calls == 1 && duplicate_calls == 0);
  esp_teardown_irq(1, 7);
  assert_empty();
}

static void native_callback(void *arg) { (*(int *)arg)++; }

static void test_native_sdk_handle_has_no_owned_adapter(void)
{
  int calls = 0;
  intr_handle_t handle;
  unsigned frees = release_calls;
  assert(esp_intr_alloc_intrstatus(1, 9, 0x1234, 0x40, native_callback,
                                  &calls, &handle) == ESP_OK);
  assert(esp_set_handle(0, ESP_SOURCE2IRQ(1), handle) == OK);
  assert(esp_get_irq(0, handle) == ESP_SOURCE2IRQ(1));
  invoke(handle);
  assert(calls == 1);
  esp_teardown_irq(1, 7);
  assert(release_calls == frees);
  assert_empty();
}

static void test_shared_cpuint_and_cpu_ownership(void)
{
  int calls[3] = {0};
  assert(setup(1, &calls[0]) == 7);
  assert(setup(2, &calls[1]) == 7);
  intr_handle_t second = esp_get_handle(0, ESP_SOURCE2IRQ(2));
  current_cpu = 1;
  assert(setup(1, &calls[2]) == 7);
  intr_handle_t other_cpu = esp_get_handle(1, ESP_SOURCE2IRQ(1));
  current_cpu = 0;
  esp_teardown_irq(1, 7);
  assert(esp_cpuint_to_irq(7, 0) == ESP_SOURCE2IRQ(2));
  assert(esp_get_irq(0, second) == ESP_SOURCE2IRQ(2));
  assert(esp_get_handle(1, ESP_SOURCE2IRQ(1)) == other_cpu);
  assert(esp_get_irq(0, other_cpu) < 0);
  invoke(second);
  invoke(other_cpu);
  assert(calls[0] == 0 && calls[1] == 1 && calls[2] == 1);
  esp_teardown_irq(2, 7);
  assert(esp_cpuint_to_irq(7, 0) < 0);
  current_cpu = 1;
  assert(esp_cpuint_to_irq(7, 1) == ESP_SOURCE2IRQ(1));
  esp_teardown_irq(1, 7);
  current_cpu = 0;
  assert_empty();
}

static void test_internal_source_uses_setup_mapping(void)
{
  int calls = 0;
  assert(setup(-3, &calls) == 7);
  intr_handle_t handle = esp_get_handle(0, ETS_INTERNAL_INTR_SOURCE_OFF - 3);
  assert(handle && esp_get_irq(0, handle) == ETS_INTERNAL_INTR_SOURCE_OFF - 3);
  invoke(handle);
  assert(calls == 1);
  esp_teardown_irq(-3, 7);
  assert_empty();
}

int main(int argc, char **argv)
{
  (void)argv;
  test_repeated_setup_teardown();
  if (argc > 1) return 0; /* Baseline control uses normal teardown only. */
  test_failed_detach_retains_callback_for_retry();
  test_allocation_failures();
  test_duplicate_irq_preserves_existing_handler();
  test_native_sdk_handle_has_no_owned_adapter();
  test_shared_cpuint_and_cpu_ownership();
  test_internal_source_uses_setup_mapping();
  puts("esp irq lifetime: cycles=200 live_bytes=0 retry/errors/native/shared/internal=pass");
  return 0;
}
