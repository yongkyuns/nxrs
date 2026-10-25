/* Mechanical SDK/default-config translation for an external reference only. */
#include "esp_err.h"
_Static_assert(ESP_ERR_INVALID_STATE == 0x103, "gain baseline unavailable status");
#define app_main capture_app_main
#include "capture-sdk.c"
#undef app_main
#include "esp_wifi_sensing.h"
#include "esp_radar.h"
_Static_assert(ESP_RADAR_TRAIN_STATUS_COMPLETE == 2, "post-training calibration admission ABI");
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_csi_gain_ctrl.h"
_Static_assert(sizeof(rx_gain_status_t) == 4 && RX_GAIN_COLLECT == 0 && RX_GAIN_READY == 1 && RX_GAIN_FORCE == 2, "gain state ABI");
_Static_assert(__builtin_types_compatible_p(__typeof__(&esp_csi_gain_ctrl_compensate_rx_gain), int (*)(void *, uint16_t, bool, float *, uint8_t, int8_t)), "compensation entry ABI");

struct presence_diagnostic {
    float wander, jitter, average, threshold;
    uint32_t templates, background, initialization, motion;
    uint8_t ready, calibrated, present, reserved;
    float background_average, basis_wander, learned_baseline;
    uint32_t train_status, train_action;
};
const uint32_t presence_c_abi[10] = {
    sizeof(struct presence_diagnostic), _Alignof(struct presence_diagnostic),
    offsetof(struct presence_diagnostic, templates), offsetof(struct presence_diagnostic, ready),
    offsetof(struct presence_diagnostic, reserved),
    offsetof(struct presence_diagnostic, background_average),
    offsetof(struct presence_diagnostic, basis_wander),
    offsetof(struct presence_diagnostic, learned_baseline),
    offsetof(struct presence_diagnostic, train_status),
    offsetof(struct presence_diagnostic, train_action)
};
extern const uint32_t presence_rust_abi[10];
extern const uint32_t presence_trace_c_abi[14], presence_trace_rust_abi[14];
extern const uint32_t presence_trace_static_bytes;
extern const uint32_t presence_pair_static_bytes;
extern const uint32_t presence_calibration_static_bytes;
extern const uint32_t presence_failure_static_bytes;
extern void presence_main(void);
void app_main(void) {
    if (presence_trace_static_bytes == 0 || presence_trace_static_bytes > 131072 ||
        presence_pair_static_bytes == 0 || presence_pair_static_bytes > 1024 ||
        presence_calibration_static_bytes == 0 || presence_calibration_static_bytes > 1024 ||
        !presence_failure_static_bytes || presence_failure_static_bytes > 65536 ||
        memcmp(reference_c_abi,reference_rust_abi,sizeof(reference_c_abi)) != 0 ||
        memcmp(presence_c_abi,presence_rust_abi,sizeof(presence_c_abi)) != 0 ||
        memcmp(presence_trace_c_abi,presence_trace_rust_abi,56) != 0) abort();
    presence_main();
}
int presence_create(void **handle) {
    esp_wifi_sensing_fsm_config_t cfg = DEFAULT_ESP_WIFI_SENSING_FSM_CONFIG();
    cfg.max_channel_num = 1;
    return esp_wifi_sensing_fsm_create(&cfg, handle);
}
int presence_diagnostic(void *handle, const uint8_t peer[6], struct presence_diagnostic *out) {
    esp_wifi_sensing_fsm_channel_diag_t d = {0};
    int status = esp_wifi_sensing_fsm_get_channel_diag(handle, peer, &d);
    if (status != ESP_OK) return status;
    *out = (struct presence_diagnostic){
        .wander=d.wander_value, .jitter=d.jitter_value,
        .average=d.presence_wander_average, .threshold=d.presence_someone_threshold,
        .templates=d.train_sample_count, .background=d.train_background_count,
        .initialization=d.init_stage, .motion=d.state, .ready=d.presence_ready,
        .calibrated=d.train_thresholds_valid, .present=d.presence_someone_status,
        .background_average=d.train_background_avg, .basis_wander=d.train_last_basis_wander,
        .learned_baseline=d.train_wander_threshold, .train_status=d.train_status,
        .train_action=d.train_last_action
    };
    return ESP_OK;
}
void *presence_task_identity(void) { return xTaskGetCurrentTaskHandle(); }
_Static_assert(sizeof(StackType_t) == 1, "IDF stack high-water units are bytes");
uint32_t presence_task_stack_free_bytes(void) { return (uint32_t)uxTaskGetStackHighWaterMark(NULL); }

#include "trace.h"
static struct presence_trace_radio trace_radio(const wifi_csi_filtered_info_t *info, float factor) {
    return (struct presence_trace_radio){
        .sequence=info->seq_id, .timestamp=info->rx_ctrl_info.timestamp,
        .rssi=info->rx_ctrl_info.rssi, .agc=info->rx_ctrl_info.agc_gain,
        .fft=info->rx_ctrl_info.fft_gain, .data_type=info->data_type,
        .length=info->valid_len, .factor=factor
    };
}
void presence_trace_rx_sdk(const uint8_t *dispatch_mac, const wifi_csi_filtered_info_t *info) {
    if (!presence_trace_active()) return;
    struct presence_trace_radio radio=trace_radio(info,0.0f);
    presence_lab_rf((const uint8_t *)info->valid_data, &radio, dispatch_mac, info->mac);
    if (!presence_trace_matches(dispatch_mac) || !presence_trace_matches(info->mac)) { presence_trace_other_rf(); return; }
    uint64_t observed=(uint64_t)esp_timer_get_time();
    radio.observed_lo=(uint32_t)observed; radio.observed_hi=(uint32_t)(observed >> 32);
    presence_trace_rf((const uint8_t *)info->valid_data, &radio);
}
void presence_trace_row_sdk(uintptr_t peer, const uint8_t *dispatch_mac, uint32_t index, const wifi_csi_filtered_info_t *info, const void *ltf, uint32_t len, const float *row, uint32_t cols, uint32_t step, bool logarithmic) {
    if (!presence_trace_active()) return;
    if (!presence_trace_matches(dispatch_mac) || !presence_trace_matches(info->mac)) return;
    struct presence_trace_radio radio=trace_radio(info,info->rx_gain_compensation);
    presence_trace_row(peer,index,(const uint8_t *)info->valid_data,&radio,ltf,len,row,cols,step,logarithmic);
}
void presence_trace_decision_sdk(uint32_t token, const uint8_t *dispatch_mac, esp_radar_handle_t peer, const wifi_radar_info_t *radar) {
    if (!presence_trace_active()) return;
    if (!presence_trace_matches(dispatch_mac)) return;
    esp_radar_train_diag_t diag={0};
    int status=esp_radar_train_get_diag_ex(peer,&diag);
    presence_trace_decision(token,status,diag.status,diag.last_action,diag.template_sample_count,diag.background_stats_count,diag.background_stats_sum,diag.last_basis_wander,radar->waveform_wander,radar->waveform_jitter);
}
const uint32_t presence_trace_c_abi[14] = {
    sizeof(struct presence_trace_radio), _Alignof(struct presence_trace_radio),
    offsetof(struct presence_trace_radio,sequence), offsetof(struct presence_trace_radio,timestamp),
    offsetof(struct presence_trace_radio,rssi), offsetof(struct presence_trace_radio,agc),
    offsetof(struct presence_trace_radio,fft), offsetof(struct presence_trace_radio,data_type),
    offsetof(struct presence_trace_radio,length), offsetof(struct presence_trace_radio,factor),
    offsetof(struct presence_trace_radio,observed_lo), offsetof(struct presence_trace_radio,observed_hi),
    WIFI_CSI_DATA_TYPE_INT8, WIFI_CSI_DATA_TYPE_INT16
};
