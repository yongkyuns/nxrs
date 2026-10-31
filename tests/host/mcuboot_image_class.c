/* Compile the actual port callback against MCUboot's UUID/FIH contract. */
#include <assert.h>
#include <stddef.h>
#include <string.h>
#include <bootutil/mcuboot_uuid.h>
#include "class.inc"

int main(void)
{
    struct image_uuid uuid = { .raw = { CONFIG_ESP_IMAGE_CLASS_ID } };
    assert(FIH_EQ(boot_uuid_cid_match(0, &uuid), FIH_SUCCESS));
    assert(FIH_NOT_EQ(boot_uuid_cid_match(1, &uuid), FIH_SUCCESS));
    assert(FIH_NOT_EQ(boot_uuid_cid_match(0, NULL), FIH_SUCCESS));
    for (size_t i = 0; i < sizeof(uuid.raw); ++i) {
        uuid.raw[i] ^= 1;
        assert(FIH_NOT_EQ(boot_uuid_cid_match(0, &uuid), FIH_SUCCESS));
        uuid.raw[i] ^= 1;
    }
    return 0;
}
