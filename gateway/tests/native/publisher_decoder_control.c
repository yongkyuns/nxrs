/* Test-only positive control: without the observer an installed H264 decoder
 * opens successfully; with the observer this process must terminate with 91.
 */
#include <libavcodec/avcodec.h>

int main(void)
{
  const AVCodec *codec = avcodec_find_decoder(AV_CODEC_ID_H264);
  if (!codec) return 93;
  AVCodecContext *context = avcodec_alloc_context3(codec);
  if (!context) return 94;
  int result = avcodec_open2(context, codec, 0);
  avcodec_free_context(&context);
  return result < 0 ? 95 : 0;
}
