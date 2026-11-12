/* SPDX-License-Identifier: MIT */
#ifndef HPI_RESEARCH_POLICY_H
#define HPI_RESEARCH_POLICY_H
#include <stdbool.h>
#include <stdint.h>

/* Admission for a NEW target. An already owned target is handled idempotently
 * by the command handler. Bits 0..5 are ECG, BioZ, wrist, finger, accelerometer and gyro. */
static inline bool hpr_start_busy(bool continuous, bool different_owner,
                                  uint8_t remote, uint8_t continuous_targets,
                                  uint8_t active_targets, bool finger_busy)
{
    return different_owner || (!continuous && remote) ||
           remote != continuous_targets || (active_targets & ~remote) ||
           (finger_busy && !(remote & 8));
}
#endif
