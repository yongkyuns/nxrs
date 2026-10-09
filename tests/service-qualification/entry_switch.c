/* SPDX-License-Identifier: MIT
 * Diagnostic only: keep both entry paths in one ELF. The C entry bypasses
 * Rust startup/argument ownership, not the independently selected workers.
 */
#include "qualification.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int sq_std_entry(int, char **);
int sq_rust_main(int argc, char **argv) {
  const char *mode = getenv("SQ_TRACE_ENTRY");
  int native = mode && strcmp(mode, "c") == 0;
  int result = native ? nxrs_sq_run(argc, argv) : sq_std_entry(argc, argv);
  printf("SQ_ENTRY_SELECTED language=%s\n", native ? "c" : "rust");
  return result;
}
