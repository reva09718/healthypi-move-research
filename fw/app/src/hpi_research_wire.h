/* SPDX-License-Identifier: MIT */
#ifndef HPI_RESEARCH_WIRE_H
#define HPI_RESEARCH_WIRE_H
#include <stddef.h>
#include <stdint.h>

#define HPR_VERSION 1
#define HPR_HEADER 16
#define HPR_STATUS_SIZE 20
#define HPR_MAX_PACKET 244
#define HPR_ECG 1
#define HPR_GSR 2
#define HPR_WRIST 3
#define HPR_FINGER 4
#define HPR_HR 5
#define HPR_TEMP 6
#define HPR_SPO2 7
#define HPR_BATTERY 8
#define HPR_WRIST_RED 9
#define HPR_WRIST_IR 10
#define HPR_FINGER_RED 11
#define HPR_ACCEL_X 12
#define HPR_ACCEL_Y 13
#define HPR_ACCEL_Z 14
#define HPR_GYRO_X 15
#define HPR_GYRO_Y 16
#define HPR_GYRO_Z 17
#define HPR_LAST_SIGNAL HPR_GYRO_Z
#define HPR_TARGET_ACCEL 5
#define HPR_TARGET_GYRO 6
#define HPR_LAST_TARGET HPR_TARGET_GYRO
#define HPR_IDLE 0
#define HPR_STARTING 1
#define HPR_WAITING 2
#define HPR_MEASURING 3
#define HPR_COMPLETE 4
#define HPR_CANCELLED 5
#define HPR_ERROR 6
#define HPR_STOPPING 7
#define HPR_FLAG_TIME_ESTIMATED 1
#define HPR_FLAG_SIGNED 2
/* Status flags only: firmware supports opcode 4 and concurrent continuous targets. */
#define HPR_CAP_CONTINUOUS 2
#define HPR_CAP_IMU 4

struct hpr_stamp {
    uint32_t first_index;
    uint32_t first_ms;
};

/* Existing IDs are a wire contract. Additional optical channels share their
 * sensor batch's index and timestamp so receivers can align them exactly by
 * boot ID + source sensor + sample index (time remains an estimate). */
static inline uint16_t hpr_rate_hz(uint8_t signal)
{
    switch (signal) {
    case HPR_ACCEL_X: case HPR_ACCEL_Y: case HPR_ACCEL_Z:
    case HPR_GYRO_X: case HPR_GYRO_Y: case HPR_GYRO_Z: return 25;
    case HPR_ECG: return 128;
    case HPR_GSR: return 32;
    case HPR_WRIST:
    case HPR_WRIST_RED:
    case HPR_WRIST_IR: return 25;
    case HPR_FINGER:
    case HPR_FINGER_RED: return 100;
    default: return 0;
    }
}

static inline void hpr_put32(uint8_t *p, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i) { p[i] = (uint8_t)(value >> (8 * i)); }
}

/* Returns zero for invalid inputs or MTU too small; otherwise bytes produced.
 * signed/unsigned int32 wire representation is identical. */
static inline size_t hpr_encode(uint8_t *out, size_t capacity, uint8_t signal,
                                uint8_t flags, uint32_t boot_id,
                                const struct hpr_stamp *stamp, const void *samples,
                                size_t offset, size_t count, uint32_t rate)
{
    if (!out || !stamp || !samples || !count || count > 255 ||
        (!rate && (count != 1 || offset != 0)) ||
        signal < 1 || signal > HPR_LAST_SIGNAL || capacity < HPR_HEADER + count * 4) { return 0; }
    out[0] = HPR_VERSION; out[1] = signal; out[2] = flags; out[3] = (uint8_t)count;
    hpr_put32(out + 4, boot_id);
    hpr_put32(out + 8, stamp->first_index + (uint32_t)offset);
    hpr_put32(out + 12, stamp->first_ms + (rate ? (uint32_t)(offset * 1000 / rate) : 0));
    /* memcpy avoids type-punning signed sample arrays through uint32_t*. */
    const uint8_t *src = (const uint8_t *)samples + offset * 4;
    for (size_t i = 0; i < count; ++i) {
        uint32_t value;
        unsigned char *v = (unsigned char *)&value;
        for (unsigned j = 0; j < 4; ++j) { v[j] = src[i * 4 + j]; }
        hpr_put32(out + HPR_HEADER + i * 4, value);
    }
    return HPR_HEADER + count * 4;
}
#endif
