/* SPDX-License-Identifier: MIT
 * Minimal command in the SAME NuttX configuration as the workload images.
 * Its linked-out queue/worker code provides an incremental app-size baseline.
 */
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv)
{
  if (argc != 2 || strcmp(argv[1], "baseline") != 0) return 1;
  puts("NUTTX_BASELINE_PASS");
  return 0;
}
