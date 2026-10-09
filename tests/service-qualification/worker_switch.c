/* SPDX-License-Identifier: MIT
 * Same-image diagnostic control. Reuse the unchanged C worker; select once
 * before the startup gate, never inside the measured event loop.
 */
#define SQ_RUST
#define nxrs_sq_worker nxrs_sq_reference_worker
#include "worker.c"
#undef nxrs_sq_worker
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

void __real_nxrs_sq_worker(struct sq_service *);
void __wrap_nxrs_sq_worker(struct sq_service *service) {
  const char *mode = getenv("SQ_TRACE_WORKER");
  int reference = mode && strcmp(mode, "c") == 0;
  if (reference) nxrs_sq_reference_worker(service);
  else __real_nxrs_sq_worker(service);
  /* After the last event and shutdown: prove selection without hot-path I/O. */
  printf("SQ_WORKER_SELECTED id=%u language=%s\n", nxrs_sq_id(service), reference ? "c" : "rust");
}
