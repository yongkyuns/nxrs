/* Synthetic SDK numerical witness only; no RF, board or production behavior. */
#include "esp_log.h"

static void positive_matrix(float samples[10][13], uint32_t seed, float scale)
{
    uint32_t value = seed;
    for (unsigned row = 0; row < 10; ++row) {
        for (unsigned col = 0; col < 13; ++col) {
            value = 1664525u * value + 1013904223u;
            samples[row][col] = (10u + ((value >> 24) % 111u)) * scale;
        }
    }
}

static void numerical_limit_tests(void)
{
    static float samples[10][13];
    struct { uint32_t before; float values[13]; uint32_t after; } output;
    static const unsigned splits[] = {10, 7, 3, 1};
    /* Independent double-precision, relative-tolerance reference in numerical.py. */
    static const float reference[13] = {
        2.00954131f, 2.66998690f, 3.67806809f, 3.67888145f, 2.92906717f,
        3.87219013f, 3.39705934f, 2.85534862f, 3.31713529f, 2.55725745f,
        3.61866180f, 2.69698254f, 2.61652589f
    };
    esp_log_level_set("esp_radar_motion_dec", ESP_LOG_DEBUG);
    for (unsigned scale_case = 0; scale_case < 2; ++scale_case) {
        positive_matrix(samples, 785, scale_case ? 0.0625f : 1.0f);
        for (unsigned split = 0; split < 4; ++split) {
            unsigned rows0 = splits[split], rows1 = 10 - rows0;
            for (unsigned repeat = 0; repeat < 3; ++repeat) {
                output.before=0x1234abcd; output.after=0x5a5abcbc;
                for (unsigned col=0;col<13;++col) output.values[col]=12345.0f;
                track_begin(0);
                int status=esp_radar_motion_dec_compute(13,rows0,samples,rows1,samples+rows0,output.values);
                unsigned allocations=track_end();
                printf("CSI_NUMERIC_CASE scale=%s split=%u,%u repeat=%u status=%d allocations=%u live=%u guards=%u\n",
                       scale_case?"1over16":"1",rows0,rows1,repeat,status,allocations,live,guard_errors);
                CHECK("numerical status",status==(scale_case?ESP_OK:ESP_FAIL));
                CHECK("numerical allocation cleanup",allocations==4&&live==0&&guard_errors==0);
                CHECK("numerical output bounds",output.before==0x1234abcd&&output.after==0x5a5abcbc);
                for (unsigned col=0;col<13;++col) {
                    CHECK("numerical output contract",scale_case?
                          closef(output.values[col],reference[col],0.00002f):output.values[col]==12345.0f);
                }
            }
        }
    }
    positive_matrix(samples,1,1.0f);
    CHECK("large scale successful control",esp_radar_motion_dec_compute(13,10,samples,0,samples+10,output.values)==ESP_OK);
    for(unsigned col=0;col<13;++col) CHECK("large scale control finite",isfinite(output.values[col]));
    puts("CSI_NUMERIC_LOGGING_END");
}
