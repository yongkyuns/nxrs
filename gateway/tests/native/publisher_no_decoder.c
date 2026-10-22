/* Test-only SDK witness. Abort if an SDK decoder actually opens successfully.
 * It observes the real native ABI; it is never linked into a product binary.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <libavcodec/avcodec.h>
#include <stdlib.h>
#include <string.h>

int avcodec_open2(AVCodecContext *context, const AVCodec *codec, AVDictionary **options)
{
  typedef int (*open_codec)(AVCodecContext *, const AVCodec *, AVDictionary **);
  void *symbol = dlsym(RTLD_NEXT, "avcodec_open2");
  open_codec call;
  _Static_assert(sizeof(call) == sizeof(symbol), "unsupported SDK witness ABI");
  if (!symbol) _Exit(92);
  memcpy(&call, &symbol, sizeof(call));
  int result = call(context, codec, options);
  if (result >= 0 && context && context->codec && av_codec_is_decoder(context->codec)) _Exit(91);
  return result;
}
