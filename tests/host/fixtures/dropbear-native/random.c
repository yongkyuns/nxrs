/* Exercise the real NuttX seeding path; SHA is an observation stub, not a
 * replacement algorithm or a cryptographic qualification.
 */
#include "includes.h"
#include <fcntl.h>
#include <setjmp.h>

static jmp_buf fatal;
static int fail_read, opens, reads, closes, hashes, burns;
static size_t hashed;
static bool stopping;

static int source_open(const char *path, int flags, ...)
{
  assert(strcmp(path, "/dev/random") == 0);
  assert(flags == (O_RDONLY | O_NONBLOCK));
  opens++;
  return 51;
}

static ssize_t source_read(int fd, void *buffer, size_t length)
{
  assert(fd == 51 && length == 32);
  reads++;
  if (fail_read)
    {
      errno = EAGAIN;
      return -1;
    }

  memset(buffer, 0xa5, length);
  return (ssize_t)length;
}

static int source_close(int fd)
{
  assert(fd == 51);
  closes++;
  return 0;
}

bool dropbear_service_stopping(void)
{
  return stopping;
}

#define open source_open
#define read source_read
#define close source_close
#include "netutils/dropbear/port/dropbear_entropy.c"
#undef open
#undef read
#undef close
#include "src/dbrandom.c"

void sha256_init(hash_state *state)
{
  memset(state, 0, sizeof(*state));
  hashes++;
}

void sha256_process(hash_state *state, const void *bytes, unsigned long length)
{
  assert(bytes != NULL);
  hashed += length;
  state->data[0] ^= ((const unsigned char *)bytes)[0];
}

void sha256_done(hash_state *state, unsigned char *output)
{
  memset(output, state->data[0], SHA256_HASH_SIZE);
}

void m_burn(void *buffer, size_t length)
{
  assert(length == SHA256_HASH_SIZE);
  memset(buffer, 0, length);
  burns++;
}

void dropbear_exit(const char *format, ...)
{
  assert(format != NULL);
  longjmp(fatal, 1);
}

int main(void)
{
  unsigned char output[32];
  assert(!donerandinit && counter == 0);
  if (!setjmp(fatal))
    {
      genrandom(output, sizeof(output));
      assert(false);
    }

  assert(opens == 0 && hashes == 0);
  fail_read = 1;
  if (!setjmp(fatal))
    {
      seedrandom();
      assert(false);
    }

  assert(opens == 1 && reads == 1 && closes == 1);
  assert(!donerandinit && hashes == 0 && burns == 1);
  fail_read = 0;
  seedrandom();
  assert(opens == 2 && reads == 2 && closes == 2);
  assert(donerandinit && counter == 0 && hashes == 1 && hashed == 64);
  assert(burns == 2 && hashpool[0] == 0xa5);
  genrandom(output, sizeof(output));
  assert(counter == 1 && hashes == 2 && burns == 3);
  assert(opens == 2 && reads == 2 && closes == 2);

  /* A failed mandatory reseed must abort, not keep using old entropy. */
  fail_read = 1;
  counter = MAX_COUNTER;
  if (!setjmp(fatal))
    {
      genrandom(output, sizeof(output));
      assert(false);
    }

  assert(opens == 3 && reads == 3 && closes == 3);
  dropbear_random_cleanup();
  assert(!donerandinit && counter == 0);
  for (size_t index = 0; index < sizeof(hashpool); index++)
    {
      assert(hashpool[index] == 0);
    }

  if (!setjmp(fatal))
    {
      genrandom(output, sizeof(output));
      assert(false);
    }

  return 0;
}
