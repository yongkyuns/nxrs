/* Fixture-only translation against the configured target header. */
#include <fcntl.h>
int entropy_probe_open_flags(void)
{
  return O_NONBLOCK;
}
