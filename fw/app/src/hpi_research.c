/* SPDX-License-Identifier: MIT
 * Opt-in research service; legacy GATT payloads remain unchanged.
 */
#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zephyr/random/random.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/atomic.h>
#include <errno.h>
#include <string.h>
#include "hpi_research.h"
#include "hpi_imu.h"
#include "hpi_research_policy.h"
#include "hpi_evt.h"
#include "hpi_dfu.h"

#define UUID_RESEARCH(n) BT_UUID_DECLARE_128(BT_UUID_128_ENCODE(0xa17e0000 + (n), \
    0x4d52, 0x4f56, 0x9100, 0x524553454152))

enum { CMD_START = 1, CMD_STOP = 2, CMD_STATUS = 3, CMD_CONTINUOUS = 4 };
enum { R_ACCEPTED, R_BUSY, R_INVALID, R_NOT_OWNER, R_UNSUPPORTED, R_DFU };
static atomic_t states[HPR_LAST_TARGET + 1];
static atomic_t indices[HPR_LAST_SIGNAL + 1];
static atomic_t remote_mask;
static atomic_t continuous_mask;
static atomic_t stop_mask;
static atomic_t finger_busy;
static atomic_t tx_failures;
static uint32_t boot_id;
static struct k_spinlock status_lock;
static uint8_t status_bytes[HPR_STATUS_SIZE];
/* Only the control thread dereferences/changes owner. */
static struct bt_conn *owner;
static uint8_t owner_targets;
static int64_t pending_since[HPR_LAST_TARGET + 1];
struct request { struct bt_conn *conn; uint8_t bytes[4]; };
K_MSGQ_DEFINE(hpr_commands, sizeof(struct request), 4, sizeof(void *));
struct scalar_packet { struct hpr_stamp stamp; int32_t value; uint8_t signal; };
K_MSGQ_DEFINE(hpr_scalars, sizeof(struct scalar_packet), 8, 4);

static bool active(uint8_t state)
{
    return state == HPR_STARTING || state == HPR_WAITING ||
           state == HPR_MEASURING || state == HPR_STOPPING;
}

bool hpi_research_continuous(uint8_t target)
{
    return target >= 1 && target <= HPR_LAST_TARGET &&
           (atomic_get(&continuous_mask) & BIT(target - 1));
}

bool hpi_research_stopping(uint8_t target)
{
    return target >= 1 && target <= HPR_LAST_TARGET &&
           (atomic_get(&stop_mask) & BIT(target - 1));
}

void hpi_research_state(uint8_t target, uint8_t state)
{
    if (target < 1 || target > HPR_LAST_TARGET) { return; }
    uint8_t old = (uint8_t)atomic_get(&states[target]);
    if (state == HPR_IDLE && old >= HPR_COMPLETE && old <= HPR_ERROR) { return; }
    if (state == HPR_IDLE && active(old)) {
        state = hpi_research_stopping(target) ? HPR_CANCELLED : HPR_ERROR;
    }
    /* A late contact update must not erase a latched Stop. */
    if (hpi_research_stopping(target) && active(state)) { state = HPR_STOPPING; }
    atomic_set(&states[target], state);
    if (!active(state)) {
        atomic_and(&remote_mask, ~BIT(target - 1));
        atomic_and(&continuous_mask, ~BIT(target - 1));
        atomic_and(&stop_mask, ~BIT(target - 1));
    }
}

bool hpi_research_remote_owned(uint8_t target)
{
    return target >= 1 && target <= HPR_LAST_TARGET &&
           (atomic_get(&remote_mask) & BIT(target - 1));
}

bool hpi_research_local_allowed(uint8_t target)
{
    ARG_UNUSED(target);
    return atomic_get(&remote_mask) == 0;
}

void hpi_research_request_stop(uint8_t target)
{
    if (target < 1 || target > HPR_LAST_TARGET) { return; }
    atomic_or(&stop_mask, BIT(target - 1));
    atomic_set(&states[target], HPR_STOPPING);
}

void hpi_research_finger_busy(bool busy) { atomic_set(&finger_busy, busy); }

void hpi_research_stamp(uint8_t signal, struct hpr_stamp *stamp, uint8_t count)
{
    if (signal < 1 || signal > HPR_LAST_SIGNAL || !stamp || !count) { return; }
    uint16_t rate = hpr_rate_hz(signal);
    stamp->first_index = (uint32_t)atomic_add(&indices[signal], count);
    /* Time of FIFO read/producer callback minus nominal batch span. NOT a
     * hardware timestamp; FIFO dwell, scheduling and sensor delays are unknown. */
    stamp->first_ms = k_uptime_get_32() - (rate ?
        ((uint32_t)(count - 1) * 1000 / rate) : 0);
}

void hpi_research_scalar(uint8_t signal, int32_t value)
{
    if (signal < HPR_HR || signal > HPR_BATTERY) { return; }
    struct scalar_packet packet = {.signal = signal, .value = value};
    hpi_research_stamp(signal, &packet.stamp, 1);
    if (k_msgq_put(&hpr_scalars, &packet, K_NO_WAIT)) { atomic_inc(&tx_failures); }
}

static ssize_t read_status(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                            void *buf, uint16_t len, uint16_t offset)
{
    uint8_t snapshot[HPR_STATUS_SIZE];
    k_spinlock_key_t key = k_spin_lock(&status_lock);
    memcpy(snapshot, status_bytes, sizeof(snapshot));
    k_spin_unlock(&status_lock, key);
    return bt_gatt_attr_read(conn, attr, buf, len, offset, snapshot, sizeof(snapshot));
}

static ssize_t write_control(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                              const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
    ARG_UNUSED(attr);
    if (offset || (flags & (BT_GATT_WRITE_FLAG_PREPARE | BT_GATT_WRITE_FLAG_CMD))) {
        return BT_GATT_ERR(BT_ATT_ERR_NOT_SUPPORTED);
    }
    if (len != 4) { return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN); }
    const uint8_t *b = buf;
    if (b[0] != HPR_VERSION || b[1] < CMD_START || b[1] > CMD_CONTINUOUS ||
        b[2] > HPR_LAST_TARGET || b[3] == 0 || (b[1] != CMD_STATUS && b[2] == 0)) {
        return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
    }
    struct request req = {.conn = bt_conn_ref(conn)};
    memcpy(req.bytes, b, sizeof(req.bytes));
    if (k_msgq_put(&hpr_commands, &req, K_NO_WAIT)) {
        bt_conn_unref(req.conn);
        return BT_GATT_ERR(BT_ATT_ERR_INSUFFICIENT_RESOURCES);
    }
    return len; /* ATT write success means queued, not acquisition success. */
}

BT_GATT_SERVICE_DEFINE(hpr_service,
    BT_GATT_PRIMARY_SERVICE(UUID_RESEARCH(0)),
    BT_GATT_CHARACTERISTIC(UUID_RESEARCH(1), BT_GATT_CHRC_WRITE,
                           BT_GATT_PERM_WRITE_ENCRYPT, NULL, write_control, NULL),
    BT_GATT_CHARACTERISTIC(UUID_RESEARCH(2), BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_READ_ENCRYPT, read_status, NULL, NULL),
    BT_GATT_CCC(NULL, BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT),
    BT_GATT_CHARACTERISTIC(UUID_RESEARCH(3), BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_NONE, NULL, NULL, NULL),
    BT_GATT_CCC(NULL, BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT)
);

static void publish_status(struct bt_conn *conn, uint8_t request_id,
                            uint8_t opcode, uint8_t target, uint8_t result)
{
    uint8_t b[HPR_STATUS_SIZE] = {HPR_VERSION, request_id, opcode, target, result};
    b[5] = target ? (uint8_t)atomic_get(&states[target]) : HPR_IDLE;
    for (uint8_t i = 1; i <= HPR_LAST_TARGET; ++i) {
        if (active((uint8_t)atomic_get(&states[i]))) { b[6] |= BIT(i - 1); }
    }
    b[7] = HPR_FLAG_TIME_ESTIMATED | HPR_CAP_CONTINUOUS | HPR_CAP_IMU;
    hpr_put32(b + 8, boot_id);
    hpr_put32(b + 12, k_uptime_get_32());
    hpr_put32(b + 16, (uint32_t)atomic_get(&tx_failures));
    k_spinlock_key_t key = k_spin_lock(&status_lock);
    memcpy(status_bytes, b, sizeof(b));
    k_spin_unlock(&status_lock, key);
    if (conn && bt_conn_get_security(conn) >= BT_SECURITY_L2 &&
        bt_gatt_is_subscribed(conn, &hpr_service.attrs[4], BT_GATT_CCC_NOTIFY)) {
        (void)bt_gatt_notify(conn, &hpr_service.attrs[4], b, sizeof(b));
    }
}

static void post_request(uint8_t target, bool start)
{
    /* Continuous ECG/BioZ is reconciled by its owning SMF thread. */
    if (target <= HPR_GSR && hpi_research_continuous(target)) { return; }
    if (target >= HPR_TARGET_ACCEL) { hpi_imu_request(target, start); return; }
    switch (target) {
    case HPR_ECG: k_event_post(&ecg_evt, start ? EVT_ECG_START : EVT_ECG_CANCEL); break;
    case HPR_GSR: k_event_post(&ecg_evt, start ? EVT_GSR_START : EVT_GSR_CANCEL); break;
    case HPR_WRIST: k_event_post(&spo2_evt, start ? EVT_SPO2_START : EVT_SPO2_CANCEL); break;
    case HPR_FINGER: k_event_post(&fi_evt, start ? EVT_FI_SPO2_START : EVT_FI_SPO2_CANCEL); break;
    }
}

static void process(struct request *req)
{
    uint8_t opcode = req->bytes[1], target = req->bytes[2], result = R_ACCEPTED;
    struct bt_conn_info info;
    if (bt_conn_get_info(req->conn, &info) || info.state != BT_CONN_STATE_CONNECTED ||
        bt_conn_get_security(req->conn) < BT_SECURITY_L2) { return; }
    if (hpi_dfu_is_active() && opcode != CMD_STOP && opcode != CMD_STATUS) { result = R_DFU; }
    else if (opcode == CMD_START && target >= HPR_TARGET_ACCEL) { result = R_UNSUPPORTED; }
    else if (opcode == CMD_START || opcode == CMD_CONTINUOUS) {
        uint32_t mask = (uint32_t)atomic_get(&remote_mask);
        uint8_t active_targets = 0;
        /* Spot checks remain exclusive. Continuous targets can coexist with
         * other continuous targets on this connection, never local spot/BP. */
        for (uint8_t i = 1; i <= HPR_LAST_TARGET; ++i) {
            if (active((uint8_t)atomic_get(&states[i]))) { active_targets |= BIT(i - 1); }
        }
        bool busy = hpr_start_busy(opcode == CMD_CONTINUOUS,
            owner && owner != req->conn, mask, atomic_get(&continuous_mask),
            active_targets, atomic_get(&finger_busy));
        if (owner == req->conn && hpi_research_remote_owned(target)) {
            if (hpi_research_stopping(target) ||
                hpi_research_continuous(target) != (opcode == CMD_CONTINUOUS)) { result = R_BUSY; }
        } else if (busy) { result = R_BUSY; }
        else {
            if (!owner) { owner = bt_conn_ref(req->conn); owner_targets = 0; }
            owner_targets |= BIT(target - 1);
            atomic_or(&remote_mask, BIT(target - 1));
            if (opcode == CMD_CONTINUOUS) { atomic_or(&continuous_mask, BIT(target - 1)); }
            atomic_and(&stop_mask, ~BIT(target - 1));
            atomic_set(&states[target], HPR_STARTING);
            pending_since[target] = k_uptime_get();
            post_request(target, true);
        }
    } else if (opcode == CMD_STOP) {
        if (owner != req->conn || !hpi_research_remote_owned(target)) { result = R_NOT_OWNER; }
        else {
            atomic_or(&stop_mask, BIT(target - 1));
            atomic_set(&states[target], HPR_STOPPING);
            pending_since[target] = k_uptime_get();
            post_request(target, false);
        }
    }
    publish_status(req->conn, req->bytes[3], opcode, target, result);
}

static void control_thread(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
    struct request req;
    int64_t next_status = 0;
    while (1) {
        if (!k_msgq_get(&hpr_commands, &req, K_MSEC(hpi_imu_active() ? 40 : 200))) {
            process(&req);
            bt_conn_unref(req.conn);
        }
        struct scalar_packet scalar;
        while (!k_msgq_get(&hpr_scalars, &scalar, K_NO_WAIT)) {
            hpi_research_send(scalar.signal, &scalar.stamp, &scalar.value, 1);
        }
        bool disconnected = false;
        bool status_due = k_uptime_get() >= next_status;
        if (status_due) { next_status = k_uptime_get() + 200; }
        if (owner) {
            struct bt_conn_info info;
            disconnected = bt_conn_get_info(owner, &info) || info.state != BT_CONN_STATE_CONNECTED;
        }
        for (uint8_t target = 1; target <= HPR_LAST_TARGET; ++target) {
            if (!hpi_research_remote_owned(target)) {
                if (owner && !disconnected && status_due && (target <= HPR_FINGER || (owner_targets & BIT(target - 1)))) { publish_status(owner, 0, 0, target, R_ACCEPTED); }
                continue;
            }
            uint8_t state = (uint8_t)atomic_get(&states[target]);
            if (disconnected || hpi_dfu_is_active() ||
                (state == HPR_STARTING && k_uptime_get() - pending_since[target] > 5000)) {
                atomic_or(&stop_mask, BIT(target - 1));
                atomic_set(&states[target], HPR_STOPPING);
            }
            /* Keep Stop latched through controller startup sleeps/cleared events.
             * Do not release ownership on a timer: actual teardown acknowledges it. */
            if (hpi_research_stopping(target)) { post_request(target, false); }
            if (owner && !disconnected && status_due) { publish_status(owner, 0, 0, target, R_ACCEPTED); }
        }
        hpi_imu_poll();
        if (owner && atomic_get(&remote_mask) == 0) {
            if (!disconnected) {
                for (uint8_t target = 1; target <= HPR_LAST_TARGET; ++target) {
                    if (target <= HPR_FINGER || (owner_targets & BIT(target - 1))) { publish_status(owner, 0, 0, target, R_ACCEPTED); }
                }
            }
            bt_conn_unref(owner); owner = NULL;
        }
    }
}
K_THREAD_DEFINE(hpr_control_thread, 1536, control_thread, NULL, NULL, NULL, 8, 0, 0);

struct send_context { uint8_t signal, count; const void *samples; const struct hpr_stamp *stamp; };
static void send_to(struct bt_conn *conn, void *user_data)
{
    const struct send_context *ctx = user_data;
    if (bt_conn_get_security(conn) < BT_SECURITY_L2 ||
        !bt_gatt_is_subscribed(conn, &hpr_service.attrs[7], BT_GATT_CCC_NOTIFY)) { return; }
    uint16_t mtu = bt_gatt_get_mtu(conn);
    size_t capacity = mtu > 3 ? MIN((size_t)(mtu - 3), (size_t)HPR_MAX_PACKET) : 0;
    if (capacity < HPR_HEADER + 4) { atomic_inc(&tx_failures); return; }
    uint8_t payload[HPR_MAX_PACKET];
    uint16_t rate = hpr_rate_hz(ctx->signal);
    uint8_t flags = HPR_FLAG_TIME_ESTIMATED |
                    ((ctx->signal <= HPR_GSR || ctx->signal == HPR_TEMP || ctx->signal >= HPR_ACCEL_X) ? HPR_FLAG_SIGNED : 0);
    size_t offset = 0, max_count = (capacity - HPR_HEADER) / 4;
    while (offset < ctx->count) {
        size_t count = MIN(max_count, (size_t)ctx->count - offset);
        size_t length = hpr_encode(payload, capacity, ctx->signal, flags, boot_id,
                                    ctx->stamp, ctx->samples, offset, count, rate);
        if (bt_gatt_notify(conn, &hpr_service.attrs[7], payload, length)) {
            atomic_inc(&tx_failures);
        }
        offset += count;
    }
}

void hpi_research_send(uint8_t signal, const struct hpr_stamp *stamp,
                       const void *samples, uint8_t count)
{
    if (signal < 1 || signal > HPR_LAST_SIGNAL || !count || !samples || !stamp || hpi_dfu_is_active()) { return; }
    struct send_context ctx = {.signal = signal, .count = count, .samples = samples, .stamp = stamp};
    bt_conn_foreach(BT_CONN_TYPE_LE, send_to, &ctx);
}

static int research_init(void)
{
    boot_id = sys_rand32_get();
    if (!boot_id) { boot_id = 1; }
    publish_status(NULL, 0, 0, 0, R_ACCEPTED);
    return 0;
}
SYS_INIT(research_init, APPLICATION, 80);
