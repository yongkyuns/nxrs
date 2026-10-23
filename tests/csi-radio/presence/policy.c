/* Host-only inputs for unchanged vendor function bodies, never HAL behavior.
 * Structures expose only the fields the selected functions need. They are not
 * SDK layouts. Allocation/logging/tasks/radio are not qualified by this test. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "vendor-constants.inc"
#define ESP_LOGI(...) ((void)0)
#define RADAR_MALLOC_RETRY(n) malloc(n)
#define ESP_OK 0
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_NOT_SUPPORTED 0x106
enum { RADAR_CALIBRATE_NO, RADAR_CALIBRATE_PROGRESS, RADAR_CALIBRATE_COMPLETE };
enum { ESP_RADAR_TRAIN_ACTION_WAIT_BUFFER, ESP_RADAR_TRAIN_ACTION_DISCARD_OUTLIER,
       ESP_RADAR_TRAIN_ACTION_COLLECT_SAMPLE, ESP_RADAR_TRAIN_ACTION_ACCUMULATE_BACKGROUND };
typedef int esp_err_t;
typedef void *esp_radar_handle_t;
typedef struct {
    unsigned calibrate_status, last_action;
    size_t buff_size, data_num;
    float waveform_jitter_buff[RADAR_BUFF_NUM], *data[CSI_WANDER_NUM];
    float none_wander, none_wander_sum, none_wander_count, static_wander;
} calibration_input;
typedef struct {
    calibration_input *cal;
    float waveform_wander_last;
    unsigned motion_dec_buff_num;
} esp_radar_peer_t;
typedef struct { float waveform_wander, waveform_jitter; } wifi_radar_info_t;
typedef struct {
    struct { float presence_sensitivity; } user_cfg;
    float presence_wander_history[HMS_PRESENCE_HISTORY_LEN];
    uint32_t presence_write_index, presence_total_samples;
    float presence_wander_average, presence_someone_threshold, wander_value, train_wander_thr;
    bool presence_ready, presence_someone_status, train_thresholds_valid;
} esp_wifi_sensing_fsm_channel_t;
static struct { struct { uint8_t dec_window_size; } dec_config; } s_ctx = {{2}};
#include "vendor-functions.inc"

static unsigned checks, failures;
#define CHECK(name, condition) do { checks++; if (!(condition)) { failures++; printf("POLICY_FAIL %s\n", name); } } while(0)

static void history(esp_wifi_sensing_fsm_channel_t *ch, float value)
{
    ch->wander_value = value;
    for (unsigned i = 0; i < HMS_PRESENCE_HISTORY_LEN; ++i) hms_update_presence_state(ch);
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "fail") == 0) {
        puts("POLICY_DELIBERATE_FAILURE"); return 1;
    }
    const float basis = 1.0f / 1024.0f; /* Exact binary fraction: no rounded tie. */
    calibration_input cal = {.calibrate_status=RADAR_CALIBRATE_PROGRESS,
                             .data_num=1, .buff_size=RADAR_BUFF_NUM};
    cal.data[0] = calloc(13, sizeof(float));
    if (!cal.data[0]) return 2;
    esp_radar_peer_t peer = {.cal=&cal, .motion_dec_buff_num=2, .waveform_wander_last=basis};
    float vector[13] = {0}; float *buffers[2] = {vector, vector};
    wifi_radar_info_t feature = {.waveform_wander=basis};
    for (unsigned i = 0; i < 16; ++i) csi_training_collect_sample(&peer, &feature, buffers, 13);
    CHECK("stationary training accumulates", cal.none_wander_count == 16);
    CHECK("stationary training keeps template", cal.data_num == 1);
    float threshold = -1, jitter = -1;
    CHECK("training stop", esp_radar_train_stop_ex(&peer,&threshold,&jitter) == ESP_OK);
    CHECK("exact stationary baseline", threshold == basis);
    CHECK("calibration marked complete", cal.calibrate_status == RADAR_CALIBRATE_COMPLETE);
    CHECK("default sensitivity gain", hms_presence_gain_from_unit(0.25f) == 8.25f);
    CHECK("minimum gain", hms_presence_gain_from_unit(0.0f) == 1.0f);
    CHECK("maximum gain", hms_presence_gain_from_unit(1.0f) == 30.0f);
    CHECK("clamp lower", hms_presence_gain_from_unit(-1.0f) == 1.0f);
    CHECK("clamp upper", hms_presence_gain_from_unit(2.0f) == 30.0f);

    esp_wifi_sensing_fsm_channel_t ch = {0};
    hms_reset_presence_state(&ch);
    CHECK("reset unavailable", !ch.presence_ready && !ch.presence_someone_status);
    ch.user_cfg.presence_sensitivity = 0.25f;
    ch.train_thresholds_valid = true; ch.train_wander_thr = threshold;
    history(&ch, basis);
    CHECK("stationary full history ready", ch.presence_ready);
    CHECK("stationary average matches training", ch.presence_wander_average == basis);
    CHECK("stationary default positive", ch.presence_someone_status);
    printf("POLICY_VALUE stationary_default_present %u\n", ch.presence_someone_status);
    CHECK("default divides baseline", ch.presence_someone_threshold == basis / 8.25f);
    ch.user_cfg.presence_sensitivity = 0.0f; history(&ch, basis);
    CHECK("minimum tie clears", !ch.presence_someone_status);
    printf("POLICY_VALUE stationary_minimum_present %u\n", ch.presence_someone_status);
    history(&ch, basis * 2);
    CHECK("above baseline minimum positive", ch.presence_someone_status);
    printf("POLICY_VALUE above_baseline_minimum_present %u\n", ch.presence_someone_status);
    ch.user_cfg.presence_sensitivity = 0.25f; history(&ch, basis / 16);
    CHECK("low feature clears at default", !ch.presence_someone_status);
    ch.train_thresholds_valid = false; history(&ch, basis * 2);
    CHECK("invalid calibration not presence", !ch.presence_someone_status);

    cal.calibrate_status = RADAR_CALIBRATE_PROGRESS;
    float before = cal.none_wander_count;
    peer.waveform_wander_last = 0.0f;
    feature.waveform_wander = basis;
    csi_training_collect_sample(&peer,&feature,buffers,13);
    CHECK("zero basis excluded from training mean", cal.none_wander_count == before);
    peer.waveform_wander_last = CSI_WANDER_THRESHOLD;
    csi_training_collect_sample(&peer,&feature,buffers,13);
    CHECK("cutoff basis included", cal.none_wander_count == before + 1);
    before = cal.none_wander_count;
    for (unsigned i = 1; i < 44; ++i) {
        peer.waveform_wander_last = CSI_WANDER_THRESHOLD * 2;
        feature.waveform_wander = 0.1f;
        csi_training_collect_sample(&peer,&feature,buffers,13);
    }
    CHECK("above cutoff excluded from training mean", cal.none_wander_count == before);
    CHECK("replacement suppresses reported training wander", feature.waveform_wander == 0.0f);
    CHECK("total collected differs from retained bank", cal.data_num == 44);
    unsigned retained = 0;
    for (unsigned i = 0; i < CSI_WANDER_NUM; ++i) if (cal.data[i]) {retained++;free(cal.data[i]);}
    CHECK("fixed size template bank", retained == 10);
    printf("POLICY_VALUE collected_template_events %u\n", (unsigned)cal.data_num);
    printf("POLICY_VALUE retained_template_slots %u\n", retained);

    float history_values[25];
    for (unsigned i=0; i<25; ++i) history_values[i] = (float)(24-i);
    CHECK("one sided trim uses lower 13", hms_trimmean(history_values,25,0.5f) == 6.0f);
    CHECK("empty trim", hms_trimmean(NULL,0,0.5f) == 0.0f);
    CHECK("null training rejected", esp_radar_train_stop_ex(NULL,&threshold,&jitter) == ESP_ERR_INVALID_ARG);
    CHECK("failed stop clears outputs", threshold == 0 && jitter == 0);
    CHECK("source retained bank limit", CSI_WANDER_NUM == 10);
    printf("POLICY_REPORT checks=%u failures=%u\n",checks,failures);
    return failures ? 1 : 0;
}
