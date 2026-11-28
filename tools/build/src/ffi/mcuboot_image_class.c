/* MCUboot authenticates and parses the class TLV; this callback supplies only
 * the configured product identity through its existing UUID/FIH interface.
 */
#include <stddef.h>
#include <string.h>
#include <bootutil/mcuboot_uuid.h>

#ifdef MCUBOOT_UUID_CID
fih_ret boot_uuid_cid_match(uint32_t image_id, const struct image_uuid *uuid_cid)
{
    static const uint8_t expected[16] = { CONFIG_ESP_IMAGE_CLASS_ID };
    FIH_DECLARE(rc, FIH_FAILURE);
    if (image_id == 0 && uuid_cid != NULL &&
        memcmp(uuid_cid->raw, expected, sizeof(expected)) == 0) {
        FIH_SET(rc, FIH_SUCCESS);
    }
    FIH_RET(rc);
}
#endif
