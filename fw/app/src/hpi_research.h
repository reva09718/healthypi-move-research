/* SPDX-License-Identifier: MIT */
#ifndef HPI_RESEARCH_H
#define HPI_RESEARCH_H
#include <stdbool.h>
#include <stdint.h>
#include "hpi_research_wire.h"

/* Call at the producer, before attempting to enqueue the sensor batch. */
void hpi_research_stamp(uint8_t signal, struct hpr_stamp *stamp, uint8_t count);
/* Called from data thread with the producer stamp, never retimestamp on send. */
void hpi_research_send(uint8_t signal, const struct hpr_stamp *stamp,
                       const void *samples, uint8_t count);
/* Scalar value converted to int32: HR=bpm, temperature=C*100, SpO2=%, battery=%. */
void hpi_research_scalar(uint8_t signal, int32_t value);
/* Hooks report actual state-machine transitions, not merely accepted commands. */
void hpi_research_state(uint8_t target, uint8_t state);
bool hpi_research_continuous(uint8_t target);
bool hpi_research_stopping(uint8_t target);
bool hpi_research_remote_owned(uint8_t target);
bool hpi_research_local_allowed(uint8_t target);
void hpi_research_request_stop(uint8_t target);
void hpi_research_finger_busy(bool busy);
#endif
