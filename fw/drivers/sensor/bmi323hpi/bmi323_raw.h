/* SPDX-License-Identifier: MIT */
#pragma once
#include <stdint.h>

/* One I2C burst: two dummy bytes, then six signed LE 16-bit axis registers. */
static inline void bmi323_decode_raw(const uint8_t bytes[14], int32_t axes[6])
{
    for (unsigned i = 0; i < 6; ++i) {
        uint16_t word = (uint16_t)bytes[2 + i * 2] |
                        ((uint16_t)bytes[3 + i * 2] << 8);
        axes[i] = word & 0x8000 ? (int32_t)word - 65536 : word;
    }
}
