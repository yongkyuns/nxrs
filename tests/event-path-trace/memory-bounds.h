/* Scalar target-header translation, not an assumed heap address range.
 * esp-hal-3rdparty b90b1837cb5ad24747deb4c895246037cc206ce5:
 * components/soc/esp32s3/include/soc/soc.h, SOC_DRAM_LOW/HIGH.
 * build.py authenticates the real SDK header and verifies these exact values.
 * External RAM is classified through the native runtime allocation bounds. */
#define P4_DRAM_LOW  0x3FC88000
#define P4_DRAM_HIGH 0x3FD00000
