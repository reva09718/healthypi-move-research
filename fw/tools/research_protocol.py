"""Reference codec for the custom HPR1 service; this does not flash the watch."""
import struct

SERVICE = "a17e0000-4d52-4f56-9100-524553454152"
CONTROL = "a17e0001-4d52-4f56-9100-524553454152"
STATUS = "a17e0002-4d52-4f56-9100-524553454152"
DATA = "a17e0003-4d52-4f56-9100-524553454152"
TARGETS = {"ecg": 1, "gsr": 2, "wrist_spo2": 3, "finger_spo2": 4, "accel": 5, "gyro": 6}
SIGNALS = {1: ("ecg", 128), 2: ("gsr", 32), 3: ("ppg_wrist", 25),
           4: ("ppg_finger", 100), 5: ("hr", None), 6: ("temp", None),
           7: ("spo2", None), 8: ("battery", None),
           9: ("ppg_wrist_red", 25), 10: ("ppg_wrist_ir", 25),
           11: ("ppg_finger_red", 100),
           12: ("accel_x", 25), 13: ("accel_y", 25), 14: ("accel_z", 25),
           15: ("gyro_x", 25), 16: ("gyro_y", 25), 17: ("gyro_z", 25)}
STATES = {0: "idle", 1: "starting", 2: "waiting_for_contact", 3: "measuring",
          4: "complete", 5: "cancelled", 6: "error", 7: "stopping"}
RESULTS = {0: "accepted", 1: "busy", 2: "invalid", 3: "not_owner",
           4: "unsupported", 5: "firmware_update_active"}


def command(action, target=0, request_id=1):
    opcode = {"start": 1, "stop": 2, "status": 3, "continuous": 4}[action]
    target = TARGETS[target] if isinstance(target, str) else target
    if target not in range(7) or (opcode != 3 and target == 0):
        raise ValueError("Invalid measurement target")
    if request_id not in range(1, 256):
        raise ValueError("request_id must be 1..255; zero is reserved")
    return bytes([1, opcode, target, request_id])


def decode_status(payload):
    if len(payload) != 20 or payload[0] != 1:
        raise ValueError("Unknown status format")
    version, rid, opcode, target, result, state, mask, flags, boot, uptime, failures = struct.unpack(
        "<8BIII", payload)
    if target > 6 or state not in STATES or result not in RESULTS:
        raise ValueError("Invalid status fields")
    return dict(version=version, request_id=rid, opcode=opcode, target=target,
                result=RESULTS[result], state=STATES[state], active_mask=mask,
                flags=flags, continuous_supported=bool(flags & 2), imu_supported=bool(flags & 4), boot_id=boot, uptime_ms=uptime, tx_failures=failures)


def decode_data(payload):
    if len(payload) < 20:
        raise ValueError("Packet too short")
    version, signal, flags, count, boot, index, millis = struct.unpack_from("<4BIII", payload)
    if version != 1 or signal not in SIGNALS or not count or len(payload) != 16 + 4 * count:
        raise ValueError("Unknown packet format or invalid length")
    signed = signal in (1, 2, 6) or signal >= 12
    if bool(flags & 2) != signed or not flags & 1 or flags & ~3:
        raise ValueError("Unexpected flags")
    if SIGNALS[signal][1] is None and count != 1:
        raise ValueError("Scalar packet must contain one value")
    values = list(struct.unpack_from("<" + ("i" if signed else "I") * count, payload, 16))
    name, hz = SIGNALS[signal]
    return dict(version=version, signal=name, signal_id=signal, boot_id=boot,
                first_sample_index=index, estimated_first_uptime_ms=millis,
                timestamp_is_estimated=True, nominal_hz=hz, wire_values=values,
                values=[v / 100 for v in values] if signal == 6 else values,
                time_basis="device-uptime/FIFO-read-backdated" if hz else "device-uptime/publication")


class Continuity:
    """Detect index gaps after a baseline. Gaps are not proof of radio loss."""
    def __init__(self):
        self.streams = {}

    def observe(self, packet):
        key = packet["signal_id"]
        previous = self.streams.get(key)
        boot, index = packet["boot_id"], packet["first_sample_index"]
        state, missing = "baseline", 0
        if previous and previous[0] == boot:
            delta = (index - previous[1]) & 0xffffffff
            if delta >= 0x80000000:
                return dict(state="duplicate_or_out_of_order", missing=None)
            missing = delta
            state = "gap" if delta else "continuous"
        elif previous:
            state = "device_reboot_or_new_boot_id"
        self.streams[key] = (boot, (index + len(packet["wire_values"])) & 0xffffffff)
        return dict(state=state, missing=missing)
