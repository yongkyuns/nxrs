// SDK witness only: production callers use the detector HAL's Rust API.
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cerrno>
#include <sanitizer/allocator_interface.h>

extern "C" void *pico_create();
extern "C" void pico_destroy(void *);
extern "C" void pico_run(void *, const int8_t *, int8_t *);

// The sanitizer build renames only the SDK's allocator calls. Product objects
// retain the ordinary allocator; this witness export exists only under tests.
static size_t allocation_count = 0, fail_at = 0;
extern "C" int test_memalign(void **output, size_t alignment, size_t bytes) noexcept
{
    if (++allocation_count == fail_at) {
        *output = nullptr;
        return ENOMEM;
    }
    return posix_memalign(output, alignment, bytes);
}

int main()
{
    std::array<int8_t, 150528> input{};
    std::array<int8_t, 33957> first_output{}, second_output{};
    // The SDK's first error diagnostic initializes libc's floating-point
    // formatter on macOS. Warm that process-owned logger before checking the
    // SDK allocation balance; the injected first failure allocates no arena.
    allocation_count = 0;
    fail_at = 1;
    assert(pico_create() == nullptr);
    const auto before = __sanitizer_get_current_allocated_bytes();
    for (size_t failure : {1, 2}) {
        allocation_count = 0;
        fail_at = failure;
        assert(pico_create() == nullptr);
        assert(__sanitizer_get_current_allocated_bytes() == before);
    }
    fail_at = 0;
    auto *first = pico_create();
    auto *second = pico_create();
    assert(first && second);
    for (int i = 0; i < 20; ++i) {
        input[i] = i;
        pico_run(first, input.data(), first_output.data());
        pico_run(second, input.data(), second_output.data());
        assert(first_output == second_output);
    }
    pico_destroy(first);
    pico_run(second, input.data(), first_output.data());
    assert(first_output == second_output);
    pico_destroy(second);
    // macOS ASan has no LeakSanitizer. Check live allocations explicitly too.
    assert(__sanitizer_get_current_allocated_bytes() == before);
    puts("PASS: ASan two independent SDK owners, 41 runs, reverse destruction, surviving owner, allocation balance and allocation-failure rollback");
}
