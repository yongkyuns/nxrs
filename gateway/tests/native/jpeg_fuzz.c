/* Test-only FFmpeg image2pipe/MJPEG component qualification. No production ABI. */
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/mem.h>

#define MAX_STREAM 524288
#define MAX_PACKET 153600
#define MAX_PIXELS (1920 * 1080)
#define MAX_ALLOC (64 * 1024 * 1024)

typedef struct {
    const uint8_t *data;
    size_t left, fragment;
} Input;
typedef struct {
    unsigned frames;
    int width, height;
    uint64_t luma;
} Observation;

static int read_input(void *opaque, uint8_t *buffer, int requested)
{
    Input *input = opaque;
    if (!input->left) return AVERROR_EOF;
    size_t amount = input->left;
    if (amount > (size_t)requested) amount = (size_t)requested;
    if (amount > input->fragment) amount = input->fragment;
    memcpy(buffer, input->data, amount);
    input->data += amount;
    input->left -= amount;
    return (int)amount;
}

static void receive(AVCodecContext *codec, AVFrame *frame, Observation *seen)
{
    for (unsigned i = 0; i < 16 && avcodec_receive_frame(codec, frame) >= 0; ++i) {
        if (frame->width > 0 && frame->height > 0 &&
            (int64_t)frame->width * frame->height <= MAX_PIXELS && frame->data[0]) {
            uint64_t hash = UINT64_C(14695981039346656037);
            for (int row = 0; row < frame->height; ++row)
                for (int column = 0; column < frame->width; ++column) {
                    hash ^= frame->data[0][(ptrdiff_t)row * frame->linesize[0] + column];
                    hash *= UINT64_C(1099511628211);
                }
            ++seen->frames;
            seen->width = frame->width;
            seen->height = frame->height;
            seen->luma = hash;
        }
        av_frame_unref(frame);
    }
}

static Observation decode(const uint8_t *data, size_t size, size_t fragment)
{
    Observation seen = {0};
    Input input = {data, size, fragment};
    AVDictionary *options = NULL;
    AVFormatContext *format = NULL;
    AVCodecContext *codec = NULL;
    AVPacket *packet = NULL;
    AVFrame *frame = NULL;
    AVIOContext *io = NULL;
    uint8_t *buffer = NULL;

    if (!size || size > MAX_STREAM) return seen;
    av_log_set_level(AV_LOG_QUIET);
    av_max_alloc(MAX_ALLOC);
    buffer = av_malloc(4096);
    if (!buffer) goto done;
    io = avio_alloc_context(buffer, 4096, 0, &input, read_input, NULL, NULL);
    if (!io) { av_free(buffer); goto done; }
    format = avformat_alloc_context();
    if (!format) goto done;
    format->pb = io;
    format->flags |= AVFMT_FLAG_CUSTOM_IO;
    format->video_codec_id = AV_CODEC_ID_MJPEG;
    /* A finite raw read size starts parsing without codec probing/seek support. */
    if (av_dict_set(&options, "frame_size", "4096", 0) < 0) goto done;
    if (avformat_open_input(&format, NULL, av_find_input_format("image2pipe"), &options) < 0)
        goto done;
    if (format->nb_streams != 1) goto done;
    codec = avcodec_alloc_context3(avcodec_find_decoder(AV_CODEC_ID_MJPEG));
    if (!codec || avcodec_parameters_to_context(codec, format->streams[0]->codecpar) < 0)
        goto done;
    codec->thread_count = 2;
    codec->max_pixels = MAX_PIXELS;
    if (avcodec_open2(codec, avcodec_find_decoder(AV_CODEC_ID_MJPEG), NULL) < 0)
        goto done;
    packet = av_packet_alloc();
    frame = av_frame_alloc();
    if (!packet || !frame) goto done;
    for (unsigned i = 0; i < 8 && av_read_frame(format, packet) >= 0; ++i) {
        if (packet->size < 0 || packet->size > MAX_PACKET) break;
        if (avcodec_send_packet(codec, packet) < 0) break;
        receive(codec, frame, &seen);
        av_packet_unref(packet);
    }
    if (avcodec_send_packet(codec, NULL) >= 0) receive(codec, frame, &seen);
done:
    av_dict_free(&options);
    av_packet_free(&packet);
    av_frame_free(&frame);
    avcodec_free_context(&codec);
    avformat_close_input(&format);
    if (io) av_freep(&io->buffer);
    avio_context_free(&io);
    return seen;
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    if (size >= 2 && size <= MAX_STREAM + 1)
        (void)decode(data + 1, size - 1, (size_t)data[0] + 1);
    return 0;
}

#ifdef NATIVE_DECODE_CONTROL
int main(int argc, char **argv)
{
    if (argc != 3) return 2;
    FILE *input = fopen(argv[2], "rb");
    uint8_t *bytes = malloc(MAX_STREAM + 1);
    if (!input || !bytes) return 2;
    size_t size = fread(bytes, 1, MAX_STREAM + 1, input);
    int failed = ferror(input);
    fclose(input);
    if (failed || size > MAX_STREAM) { free(bytes); return 2; }
    Observation seen = decode(bytes, size, 1);
    free(bytes);
    if (strcmp(argv[1], "valid") == 0) {
        if (seen.frames != 1 || seen.width != 320 || seen.height != 240) return 3;
        printf("NATIVE_JPEG_CONTROL_PASS valid frames=%u width=%d height=%d luma=%016" PRIx64 "\n",
               seen.frames, seen.width, seen.height, seen.luma);
    } else if (strcmp(argv[1], "invalid") == 0) {
        if (seen.frames != 0) return 3;
        puts("NATIVE_JPEG_CONTROL_PASS invalid");
    } else return 2;
    return 0;
}
#endif
