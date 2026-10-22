// SDK witness only: production callers use the detector HAL's Rust API.
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>

extern "C" void *nxrs_pico_create();
extern "C" void nxrs_pico_destroy(void *);
extern "C" void nxrs_pico_run(void *, const int8_t *, int8_t *);

int main()
{
    std::array<int8_t, 150528> input{};
    std::array<int8_t, 33957> first_output{}, second_output{};
    auto *first = nxrs_pico_create();
    auto *second = nxrs_pico_create();
    assert(first && second);
    for (int i = 0; i < 20; ++i) {
        input[i] = i;
        nxrs_pico_run(first, input.data(), first_output.data());
        nxrs_pico_run(second, input.data(), second_output.data());
        assert(first_output == second_output);
    }
    nxrs_pico_destroy(first);
    nxrs_pico_run(second, input.data(), first_output.data());
    assert(first_output == second_output);
    nxrs_pico_destroy(second);
    puts("PASS: ASan two independent SDK owners, 41 runs, reverse destruction and surviving owner");
}
