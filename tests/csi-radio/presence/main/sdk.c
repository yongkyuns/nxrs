/* Mechanical SDK/default-config translation for an external reference only. */
#define app_main capture_app_main
#include "capture-sdk.c"
#undef app_main
#include "esp_wifi_sensing.h"
#include "freertos/task.h"

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
extern void presence_main(void);
void app_main(void) {
    if (memcmp(reference_c_abi,reference_rust_abi,sizeof(reference_c_abi)) != 0 ||
        memcmp(presence_c_abi,presence_rust_abi,sizeof(presence_c_abi)) != 0) abort();
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
