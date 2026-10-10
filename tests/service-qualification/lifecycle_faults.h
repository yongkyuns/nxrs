/* SPDX-License-Identifier: MIT
 * Shared shutdown fault hooks for lifecycle qualification targets. */
#ifndef NXRS_SERVICE_LIFECYCLE_FAULTS_H
#define NXRS_SERVICE_LIFECYCLE_FAULTS_H

enum { SQ_FAULT_STOP, SQ_FAULT_JOIN, SQ_FAULT_CLOSE };

void sq_fault_arm(unsigned kind, unsigned position);
void sq_fault_disarm(void);
unsigned sq_fault_hits(void);
unsigned sq_fault_joined(void);
unsigned sq_fault_closed(void);

#endif
