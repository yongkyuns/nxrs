/* SPDX-License-Identifier: MIT */
#ifndef NXRS_EVENT_SERVICES_HAL_H
#define NXRS_EVENT_SERVICES_HAL_H

/* Drive the onboard GPIO2 LED. Return 0 on success and -1 on failure.
 * `level` is the physical pin level: 0 is off and 1 is on.
 */
int es_hal_init(void);
int es_hal_apply(unsigned level);
int es_hal_close(void);

#endif
