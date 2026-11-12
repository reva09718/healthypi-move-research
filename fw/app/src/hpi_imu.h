/* SPDX-License-Identifier: MIT */
#pragma once
#include <stdbool.h>
#include <stdint.h>
void hpi_imu_request(uint8_t target, bool start);
void hpi_imu_poll(void);
bool hpi_imu_active(void);
