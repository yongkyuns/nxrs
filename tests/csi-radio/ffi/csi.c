#include "esp_idf_version.h"
#if ESP_IDF_VERSION_MAJOR != 6 || ESP_IDF_VERSION_MINOR != 1
#error "This witness requires the pinned IDF 6.1-derived headers"
#endif
/* SPDX-License-Identifier: MIT. Mechanical pinned-SDK witness, test image only. */
#include "csi.h"
#include <stddef.h>
#include <string.h>
#include "esp_wifi.h"
#include "esp_csi_gain_ctrl.h"
_Static_assert(sizeof(struct nxrs_csi_metadata) == 92, "Rust/C metadata size");
_Static_assert(offsetof(struct nxrs_csi_metadata, rx_ctrl) == 28, "metadata offset");
_Static_assert(sizeof(wifi_pkt_rx_ctrl_t) <= 64, "RX control capture capacity");
_Static_assert(sizeof(wifi_pkt_rx_ctrl_t) >= 24, "gain extraction byte bounds");

/* Only this explicitly linked qualification image opts in during native init.
 * The real NuttX adapter still owns station initialization and its OS callbacks. */
esp_err_t __real_esp_wifi_init(const wifi_init_config_t *config);
esp_err_t __wrap_esp_wifi_init(const wifi_init_config_t *config)
{
  wifi_init_config_t enabled = *config;
  enabled.csi_enable = 1;
  return __real_esp_wifi_init(&enabled);
}

const int8_t *nxrs_csi_test_translate(const wifi_csi_info_t *info,
                                    struct nxrs_csi_metadata *output)
{
  struct nxrs_csi_metadata m = {0};
  if (info == NULL || info->buf == NULL || output == NULL) return NULL;
  m.timestamp_us = info->rx_ctrl.timestamp;
  m.rx_ctrl_len = sizeof(info->rx_ctrl);
  m.len = info->len;
  memcpy(m.mac, info->mac, sizeof(m.mac));
  m.rssi = info->rx_ctrl.rssi;
  m.noise_floor = info->rx_ctrl.noise_floor;
  m.channel = info->rx_ctrl.channel;
  m.secondary_channel = info->rx_ctrl.secondary_channel;
  m.sig_mode = info->rx_ctrl.sig_mode;
  m.bandwidth = info->rx_ctrl.cwb;
  m.mcs = info->rx_ctrl.mcs;
  m.stbc = info->rx_ctrl.stbc;
  m.first_word_invalid = info->first_word_invalid;
  memcpy(m.rx_ctrl, &info->rx_ctrl, sizeof(info->rx_ctrl));
  /* Fixed byte extraction only. Interpretation under this radio is under test. */
  esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &m.agc_gain, &m.fft_gain);
  *output = m;
  return info->buf;
}

int nxrs_csi_test_configure(void)
{
  const wifi_csi_config_t config = {
    .lltf_en = true, .htltf_en = false, .stbc_htltf2_en = false,
    .ltf_merge_en = true, .channel_filter_en = true,
    .manu_scale = false, .shift = 0, .dump_ack_en = false
  };
  return esp_wifi_set_csi_config(&config);
}
int nxrs_csi_test_register(uint32_t epoch, nxrs_csi_native_callback callback)
{
  return esp_wifi_set_csi_rx_cb(epoch ? callback : NULL, (void *)(uintptr_t)epoch);
}
int nxrs_csi_test_enable(bool enabled) { return esp_wifi_set_csi(enabled); }
int nxrs_csi_test_force(uint8_t agc, int8_t fft) { return esp_csi_gain_ctrl_set_rx_force_gain(agc, fft); }
int nxrs_csi_test_record(uint8_t agc, int8_t fft) { return esp_csi_gain_ctrl_record_rx_gain(agc, fft); }
int nxrs_csi_test_baseline(uint8_t *agc, int8_t *fft) { return esp_csi_gain_ctrl_get_rx_gain_baseline(agc, fft); }
int nxrs_csi_test_compensation(float *factor, uint8_t agc, int8_t fft) { return esp_csi_gain_ctrl_get_gain_compensation(factor, agc, fft); }
void nxrs_csi_test_reset(void) { esp_csi_gain_ctrl_reset_rx_gain_baseline(); }
