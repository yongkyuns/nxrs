/* Test-only, mechanical translation of the pinned radar SDK headers. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_radar.h"
struct presence_trace_radio {
    uint32_t sequence, timestamp;
    int32_t rssi;
    uint32_t agc;
    int32_t fft;
    uint32_t data_type, length;
    float factor;
    uint32_t observed_lo, observed_hi;
};
bool presence_trace_active(void);
void presence_trace_rf(const uint8_t *, const struct presence_trace_radio *);
void presence_trace_row(uintptr_t, uint32_t, const uint8_t *, const struct presence_trace_radio *, const uint8_t *, uint32_t, const float *, uint32_t, uint32_t, bool);
uint32_t presence_trace_enqueue(uintptr_t, uint32_t, uint32_t, uint32_t, uint32_t, const uint32_t *, const uint32_t *);
int presence_trace_compute(uint32_t, uint32_t, uint32_t, uint32_t, float *);
void presence_trace_basis(uint32_t, float);
void presence_trace_decision(uint32_t, int32_t, uint32_t, uint32_t, uint32_t, uint32_t, float, float, float, float);
void presence_trace_rx_sdk(const wifi_csi_filtered_info_t *);
void presence_trace_row_sdk(uintptr_t, uint32_t, const wifi_csi_filtered_info_t *, const void *, uint32_t, const float *, uint32_t, uint32_t, bool);
void presence_trace_decision_sdk(uint32_t, esp_radar_handle_t, const wifi_radar_info_t *);
