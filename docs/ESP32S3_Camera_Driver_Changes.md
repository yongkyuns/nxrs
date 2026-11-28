# Historical ESP32-S3 camera driver investigation

> **Retired implementation.** This document preserves the original private-driver
> investigation and its measurements. The unused `ESP32S3_CAMERA` driver and its
> configuration option have been removed. Current Freenove profiles use NuttX's
> standard capture queue, the ESP32-S3 `ESP32S3_CAM` lower half and the OV3660 sensor
> driver. See the [camera HAL](../hal/camera/README.md) and
> [maintained patch inventory](upstream-patchsets.md) for the supported path.

## Overview

This document summarizes the changes made to the ESP32-S3 camera driver (`arch/xtensa/src/esp32s3/esp32s3_camera.c`) for NuttX to support the Freenove ESP32-S3-WROOM CAM board with OV3660 sensor.

## Hardware Configuration

- **Board:** Freenove ESP32-S3-WROOM CAM
- **Sensor:** OV3660 (3MP) at I2C address 0x3C
- **Interface:** 8-bit parallel camera interface via LCD_CAM peripheral
- **Resolution:** QVGA (320x240) RGB565

### Pin Configuration
| Signal | GPIO |
|--------|------|
| XCLK   | 15   |
| PCLK   | 13   |
| VSYNC  | 6    |
| HREF   | 7    |
| D0-D7  | 11, 9, 8, 10, 12, 18, 17, 16 |
| SDA    | 4    |
| SCL    | 5    |

## Key Technical Fixes

### 1. XCLK Generation via LCD_CAM (not LEDC)

ESP32-S3 uses the LCD_CAM module for camera XCLK generation, unlike ESP32/ESP32-S2 which use LEDC.

```c
/* Configure LCD_CAM_CAM_CTRL_REG for XCLK generation */
regval = (2 << LCD_CAM_CAM_CLK_SEL_S) |           /* CLK160 source */
         (clk_div << LCD_CAM_CAM_CLKM_DIV_NUM_S); /* Integer divider */
putreg32(regval, LCD_CAM_CAM_CTRL_REG);
```

### 2. 8-bit Camera Mode Configuration

The camera has 8 data pins, requiring `CAM_2BYTE_EN = 0`:

```c
regval = ((4095) << LCD_CAM_CAM_REC_DATA_BYTELEN_S) |
         (1 << LCD_CAM_CAM_LINE_INT_NUM_S) |
         LCD_CAM_CAM_VSYNC_FILTER_EN |
         LCD_CAM_CAM_CLK_INV |          /* Invert PCLK for data sampling */
         LCD_CAM_CAM_VSYNC_INV;         /* Invert VSYNC for EOF detection */
putreg32(regval, LCD_CAM_CAM_CTRL1_REG);
```

### 3. PCLK and VSYNC Polarity Inversion

Added signal inversions for proper data capture timing:
- `CAM_CLK_INV`: Sample data on falling PCLK edge
- `CAM_VSYNC_INV`: Proper frame boundary detection

### 4. DMA Initialization Using Proper API

Changed from manual register manipulation to using `esp32s3_dma_enable()`:

```c
/* Start DMA using the proper API */
esp32s3_dma_enable(priv->dma_channel, false);
```

### 5. Frame Completion Detection via Byte Counting

Added fallback detection when `DMA_IN_SUC_EOF` isn't generated:

```c
/* Check if we've received enough data by counting descriptors */
int total_bytes = 0;
for (int i = 0; i < CAM_DMA_DESC_COUNT; i++) {
    uint32_t ctrl = priv->dma_desc[i].ctrl;
    uint32_t owner = (ctrl >> 31) & 1;
    uint32_t length = (ctrl >> 12) & 0xfff;
    if (owner == 0)  /* CPU owns it, DMA has written */
        total_bytes += length;
    else
        break;
}
if (total_bytes >= (int)priv->frame_buffer_size) {
    /* Frame complete */
}
```

### 6. OV3660 Sensor Support

Added 16-bit register addressing for OV3660 sensor (vs 8-bit for OV2640):

```c
static int sccb_write_reg16(uint8_t addr, uint16_t reg, uint8_t val);
static int sccb_read_reg16(uint8_t addr, uint16_t reg, uint8_t *val);
```

### 7. SCCB Bit-Banging for Sensor Communication

Implemented software SCCB (I2C-like) protocol to bypass NuttX I2C driver issues:

```c
static void sccb_init(int sda_pin, int scl_pin);
static int sccb_write_reg(uint8_t addr, uint8_t reg, uint8_t val);
static int sccb_read_reg(uint8_t addr, uint8_t reg, uint8_t *val);
```

## Code Cleanup

### Removed Unused Code
- `cam_i2c_write_addr()`, `cam_i2c_write()`, `cam_i2c_read_addr()`, `cam_i2c_read()`
- `cam_try_sensor_addr()`, `cam_i2c_scan()`
- Unused variables: `dma_reg_offset`, `device_count`

### Fixed Compiler Warnings
- Shadow variable warning: renamed inner `ret` to `ack` in I2C scan loop

### Reduced Debug Verbosity
- Changed `printf()` to `_info()` / `_err()` for proper NuttX logging
- Removed verbose GPIO check, I2C scan, and register dump output
- Simplified initialization messages

## Test Results

```
[CAM-CAPTURE] Starting capture...
[CAM-CAPTURE] DMA setup: 153600 bytes
[CAM-CAPTURE] Frame received via byte count! total=158472
```

- Successfully captures QVGA RGB565 frames (~153KB expected, ~158KB received)
- OV3660 sensor detected and initialized at 0x3C
- Frame capture completes reliably

## File Changes

| File | Changes |
|------|---------|
| `arch/xtensa/src/esp32s3/esp32s3_camera.c` | Main driver implementation |
| `arch/xtensa/src/esp32s3/esp32s3_camera.h` | Header file (unchanged) |
| `boards/.../esp32s3_bringup.c` | Camera initialization with Freenove pin config |

## Configuration

Enable in NuttX menuconfig:
```
CONFIG_ESP32S3_CAMERA=y
CONFIG_ESP32S3_I2C=y
CONFIG_ESP32S3_I2C0=y
```

## Known Limitations

1. Only RGB565 format tested (JPEG not yet verified)
2. Only QVGA resolution tested
3. Sensor settings (brightness, contrast, etc.) are stubs
4. V4L2 ioctl interface not yet implemented
