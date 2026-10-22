/* SPDX-License-Identifier: MIT. SDK witness only; never a production HAL API. */
#pragma once
#include <stdint.h>
#include <stdbool.h>
struct nxrs_csi_metadata {
  uint32_t timestamp_us;
  uint16_t rx_ctrl_len, len;
  uint8_t mac[6];
  int8_t rssi, noise_floor;
  uint8_t channel, secondary_channel, sig_mode, bandwidth, mcs, stbc;
  uint8_t first_word_invalid, agc_gain;
  int8_t fft_gain;
  uint8_t reserved[3], rx_ctrl[64];
};
struct wifi_csi_info_t;
typedef void (*nxrs_csi_native_callback)(void *, struct wifi_csi_info_t *);
const int8_t *nxrs_csi_test_translate(const struct wifi_csi_info_t *, struct nxrs_csi_metadata *);
int nxrs_csi_test_configure(void);
int nxrs_csi_test_register(uint32_t epoch, nxrs_csi_native_callback callback);
int nxrs_csi_test_enable(bool enabled);
int nxrs_csi_test_force(uint8_t agc, int8_t fft);
int nxrs_csi_test_record(uint8_t agc, int8_t fft);
int nxrs_csi_test_baseline(uint8_t *, int8_t *);
int nxrs_csi_test_compensation(float *, uint8_t, int8_t);
void nxrs_csi_test_reset(void);
