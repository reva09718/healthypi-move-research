/* SPDX-License-Identifier: MIT */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <errno.h>
#include "hpi_imu.h"
#include "hpi_research.h"
#include "hpi_dfu.h"
#include "bmi323_hpi.h"

/* Reuses the research control thread: no additional thread stack or workqueue.
 * Sensors run at 50 Hz; snapshot polling emits nominal 25 Hz (not a FIFO log).
 * Five samples/axis per packet keep BLE notification overhead bounded. */
#define IMU_BATCH 5
static const struct device *const imu = DEVICE_DT_GET(DT_NODELABEL(bmi323));
static struct {
    struct hpr_stamp stamp;
    int32_t samples[3][IMU_BATCH];
    uint8_t count;
} batches[2];
static uint8_t running, failures, error_mask;
static int64_t next_sample;

bool hpi_imu_active(void) { return running != 0; }

static void flush(unsigned group)
{
    if (!batches[group].count) { return; }
    uint8_t first = group ? HPR_GYRO_X : HPR_ACCEL_X;
    for (unsigned axis = 0; axis < 3; ++axis) {
        hpi_research_send(first + axis, &batches[group].stamp,
                          batches[group].samples[axis], batches[group].count);
    }
    batches[group].count = 0;
}

void hpi_imu_request(uint8_t target, bool start)
{
    if (target < HPR_TARGET_ACCEL || target > HPR_TARGET_GYRO) { return; }
    unsigned group = target - HPR_TARGET_ACCEL;
    uint8_t bit = BIT(group);
    if (start) {
        if (running & bit) { return; }
        /* No configuration was touched if the device never initialized. */
        if (!device_is_ready(imu)) { hpi_research_state(target, HPR_ERROR); return; }
        int ret = bmi323_hpi_research_start(imu, group == 1);
        if (ret) {
            if (group && bmi323_hpi_research_stop_gyro(imu)) {
                running |= bit;
                error_mask |= bit;
                hpi_research_request_stop(target);
            } else { hpi_research_state(target, HPR_ERROR); }
            return;
        }
        if (!running) { next_sample = k_uptime_get(); failures = 0; }
        batches[group].count = 0;
        running |= bit;
        /* Actual MEASURING is reported only after a successful sample read. */
    } else {
        if (group && bmi323_hpi_research_stop_gyro(imu)) { return; }
        flush(group);
        running &= ~bit;
        hpi_research_state(target, (error_mask & bit) ? HPR_ERROR : HPR_CANCELLED);
        error_mask &= ~bit;
    }
}

void hpi_imu_poll(void)
{
    int64_t now = k_uptime_get();
    if (!running || now < next_sample || hpi_dfu_is_active()) { return; }
    /* Do not invent catch-up samples after scheduler/BLE delays. */
    next_sample = now + 40;
    int32_t axes[6];
    if (bmi323_hpi_research_read(imu, axes)) {
        if (++failures >= 3) {
            for (unsigned group = 0; group < 2; ++group) {
                if (running & BIT(group)) {
                    error_mask |= BIT(group);
                    hpi_research_request_stop(HPR_TARGET_ACCEL + group);
                }
            }
        }
        return;
    }
    failures = 0;
    for (unsigned group = 0; group < 2; ++group) {
        uint8_t target = HPR_TARGET_ACCEL + group;
        if (!(running & BIT(group)) || hpi_research_stopping(target)) { continue; }
        struct hpr_stamp stamp;
        hpi_research_stamp(group ? HPR_GYRO_X : HPR_ACCEL_X, &stamp, 1);
        if (!batches[group].count) { batches[group].stamp = stamp; }
        for (unsigned axis = 0; axis < 3; ++axis) {
            batches[group].samples[axis][batches[group].count] = axes[group * 3 + axis];
        }
        if (++batches[group].count == IMU_BATCH) { flush(group); }
        hpi_research_state(target, HPR_MEASURING);
    }
}
