/* Test-only negative controls proving the configured sanitizer runtimes fail. */
#include <stdlib.h>
int main(int argc, char **argv)
{
  (void)argv;
  if (argc == 2)
    {
      volatile unsigned char *bytes = malloc(8);
      if (!bytes) return 2;
      bytes[8] = 1;
      free((void *)bytes);
    }
  else
    {
      volatile unsigned shift = 32;
      volatile unsigned value = 1u << shift;
      (void)value;
    }
  return 0;
}
