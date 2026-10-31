/* Compile the pinned upstream admission policy, without a second implementation. */
#include <assert.h>
#include <stdint.h>
#define MCUBOOT_DOWNGRADE_PREVENTION
#define MCUBOOT_SWAP_USING_SCRATCH
#define MCUBOOT_DOWNGRADE_PREVENTION_SECURITY_COUNTER 1
#define BOOT_CURR_IMG(state) 0
#define BOOT_IMG_AREA(state, slot) ((void *)(uintptr_t)((slot) + 1))
#define BOOT_SLOT_PRIMARY 0
#define BOOT_SLOT_SECONDARY 1
#define BOOT_LOG_INF(...) ((void)0)
struct image_header { unsigned ih_ver; };
struct boot_loader_state {
    uint32_t counter[2];
    unsigned missing;
    struct image_header header[2];
};
#define boot_img_hdr(state, slot) (&(state)->header[slot])
static unsigned scrambles;
static int bootutil_get_img_security_cnt(struct boot_loader_state *state,
                                        int slot, void *area, uint32_t *counter)
{
    assert(area == BOOT_IMG_AREA(state, slot));
    *counter = state->counter[slot];
    return state->missing & (1u << slot) ? -1 : 0;
}
static void boot_scramble_slot(void *area, int slot)
{
    assert(slot == 1 && area == BOOT_IMG_AREA(0, slot));
    ++scrambles;
}
static int boot_version_cmp(const unsigned *a, const unsigned *b)
{
    (void)a; (void)b;
    assert(0 && "firmware version must not replace security epoch admission");
    return -1;
}
#include "counter.inc"
int main(void)
{
    const struct { uint32_t current, candidate; unsigned missing; int result; } cases[] = {
        {2, 1, 0, -1}, {2, 2, 0, 0}, {2, 3, 0, 0},
        {2, 0, 2, -1}, {0, 0, 1, 0}, /* Upstream legacy-primary migration. */
        {UINT32_MAX, UINT32_MAX - 1, 0, -1},
        {UINT32_MAX, UINT32_MAX, 0, 0},
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        struct boot_loader_state state = {
            .counter = {cases[i].current, cases[i].candidate},
            .missing = cases[i].missing,
        };
        scrambles = 0;
        assert(check_downgrade_prevention(&state) == cases[i].result);
        assert(scrambles == (cases[i].result < 0 ? 1u : 0u));
    }
    return 0;
}
