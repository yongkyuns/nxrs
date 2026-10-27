/* Owned synthetic API regression. No media parser, external files or networking. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <x264.h>

static unsigned freed;
static void release_param(void *param) { ++freed; free(param); }
static void check(int condition) { if (!condition) exit(2); }

static void parameters(x264_param_t *param, int depth)
{
    check(x264_param_default_preset(param, "ultrafast", "zerolatency") == 0);
    param->i_width = param->i_height = 16;
    param->i_threads = 1;
    param->i_log_level = X264_LOG_NONE;
    param->i_bitdepth = depth;
    param->i_csp = X264_CSP_I420 | (depth == 10 ? X264_CSP_HIGH_DEPTH : 0);
}

static void refused(int depth, unsigned kind)
{
    x264_param_t local, *param = kind == 2 ? malloc(sizeof(*param)) : &local;
    check(param != NULL); parameters(param, depth);
    if (kind != 0) check(x264_param_parse(param, "stats", "owned-synthetic-stat-name") == 0);
    if (kind == 2) param->param_free = release_param;
    if (kind == 3) check(x264_param_parse(param, "cqmfile", "missing-owned-qualification-matrix") == 0);
    else param->i_width = 0;
    x264_t *encoder = x264_encoder_open(param);
    check(encoder == NULL);
    if (kind != 2) {
        if (kind != 0) check(strcmp(param->rc.psz_stat_in, "owned-synthetic-stat-name") == 0);
        x264_param_cleanup(param); /* Caller ownership survives failed construction. */
    }
}

static void encoded(int depth)
{
    x264_param_t param; parameters(&param, depth);
    check(x264_param_parse(&param, "stats", "owned-success-stat-name") == 0);
    x264_t *encoder = x264_encoder_open(&param); check(encoder != NULL);
    check(strcmp(param.rc.psz_stat_out, "owned-success-stat-name") == 0);
    x264_param_cleanup(&param);
    x264_picture_t image, output;
    check(x264_picture_alloc(&image, X264_CSP_I420 | (depth == 10 ? X264_CSP_HIGH_DEPTH : 0), 16, 16) == 0);
    for (int plane = 0; plane < 3; ++plane) {
        int rows = plane ? 8 : 16;
        memset(image.img.plane[plane], 0, (size_t)image.img.i_stride[plane] * rows);
    }
    image.i_pts = 0;
    x264_nal_t *nals = NULL; int count = 0;
    check(x264_encoder_encode(encoder, &nals, &count, &image, &output) > 0 && count > 0);
    x264_picture_clean(&image); x264_encoder_close(encoder);
}

int main(void)
{
    unsigned refusals = 0;
    for (int depth = 8; depth <= 10; depth += 2) {
        for (unsigned round = 0; round < 8; ++round)
            for (unsigned kind = 0; kind < 4; ++kind) { refused(depth, kind); ++refusals; }
        encoded(depth);
    }
    check(refusals == 64 && freed == 16);
    printf("X264_OPEN_CLEANUP {\"constructor_refusals\":%u,\"successful_encodes\":2,\"param_free_calls\":%u}\n", refusals, freed);
    check(fflush(stdout) == 0);
    return 0;
}
