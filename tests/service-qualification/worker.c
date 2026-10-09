/* SPDX-License-Identifier: MIT */
#include "qualification.h"

void nxrs_sq_worker(struct sq_service *service) {
  unsigned id = nxrs_sq_id(service), count = nxrs_sq_count(service);
  int led = id == count - 2;
  struct sq_event event, pending;
  uint32_t next_control = 0, next_data = 1;
  int has_pending = 0;
  if (led && nxrs_sq_led_open(service) != 0) {
    nxrs_sq_failed(service);
    return;
  }
  nxrs_sq_ready(service);
  for (;;) {
    int result = nxrs_sq_wait(service, has_pending ? &pending : NULL, &event);
    has_pending = 0;
    if (result < 0) { nxrs_sq_failed(service); break; }
    if (result == 0) break;
    if (event.source != 0 || event.reserved != 0 ||
        (event.kind != SQ_CONTROL && event.kind != SQ_DATA) ||
        event.value != (event.sequence & 1u)) {
      nxrs_sq_failed(service);
      break;
    }
    uint32_t *next = event.kind == SQ_CONTROL ? &next_control : &next_data;
    if (event.sequence != *next) { nxrs_sq_failed(service); break; }
    *next += event.kind == SQ_CONTROL ? 3u : (*next % 3u == 1 ? 1u : 2u);
    if (led && nxrs_sq_led_apply(service, event.value) != 0) {
      nxrs_sq_failed(service);
      break;
    }
    nxrs_sq_record(service, &event);
    if (id + 1 < count) { pending = event; has_pending = 1; }
  }
  if (led && nxrs_sq_led_close(service) != 0) nxrs_sq_failed(service);
}

#ifndef SQ_RUST
int main(int argc, char **argv) { return nxrs_sq_run(argc, argv); }
#endif
