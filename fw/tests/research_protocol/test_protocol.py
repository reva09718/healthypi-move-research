"""Run: python -m unittest discover -s fw/tests/research_protocol -p test_protocol.py -v"""
from pathlib import Path
import struct
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from research_protocol import command, decode_data, decode_status, Continuity


class ProtocolTests(unittest.TestCase):
    def packet(self, index=0, boot=42, signal=1, values=(-12, 1000), millis=1000):
        flags = 3 if signal in (1, 2, 6) or signal >= 12 else 1
        return decode_data(struct.pack("<4BIII", 1, signal, flags, len(values), boot, index, millis) +
                           struct.pack("<" + ("i" if flags == 3 else "I") * len(values), *values))

    def test_commands(self):
        self.assertEqual(command("start", "ecg", 7), bytes([1, 1, 1, 7]))
        self.assertEqual(command("stop", "finger_spo2", 8), bytes([1, 2, 4, 8]))
        self.assertEqual(command("continuous", "gsr", 9), bytes([1, 4, 2, 9]))
        with self.assertRaises(ValueError): command("start", 0)
        with self.assertRaises(ValueError): command("status", 0, 0)

    def test_values(self):
        self.assertEqual(self.packet()["values"], [-12, 1000])
        self.assertEqual(self.packet(signal=6, values=(3341,))["values"], [33.41])
        self.assertEqual(self.packet(signal=3, values=(4000000000,))["values"], [4000000000])
        with self.assertRaises(ValueError): decode_data(bytes(20))
        with self.assertRaises(ValueError): decode_data(bytes(19))

    def test_continuity(self):
        tracker = Continuity()
        self.assertEqual(tracker.observe(self.packet(100))["state"], "baseline")
        self.assertEqual(tracker.observe(self.packet(102))["missing"], 0)
        self.assertEqual(tracker.observe(self.packet(107))["missing"], 3)
        self.assertEqual(tracker.observe(self.packet(107))["state"], "duplicate_or_out_of_order")
        self.assertEqual(tracker.observe(self.packet(109))["state"], "continuous")
        self.assertEqual(tracker.observe(self.packet(0, boot=43))["state"], "device_reboot_or_new_boot_id")

    def test_counter_wrap(self):
        tracker = Continuity()
        tracker.observe(self.packet(0xfffffffe))
        self.assertEqual(tracker.observe(self.packet(0))["state"], "continuous")

    def test_status(self):
        status = decode_status(struct.pack("<8BIII", 1, 7, 1, 1, 0, 2, 1, 1, 42, 999, 0))
        self.assertEqual(status["state"], "waiting_for_contact")
        self.assertEqual(status["request_id"], 7)

    def test_continuous_capability_and_simultaneous_mask(self):
        status = decode_status(struct.pack("<8BIII", 1, 9, 4, 2, 0, 3, 15, 3, 42, 999, 0))
        self.assertTrue(status["continuous_supported"])
        self.assertEqual(status["active_mask"], 15)

    def test_additional_ppg_channels(self):
        for signal, name, hz in ((9, "ppg_wrist_red", 25),
                                 (10, "ppg_wrist_ir", 25),
                                 (11, "ppg_finger_red", 100)):
            with self.subTest(signal=signal):
                p = self.packet(signal=signal, values=(0, 4000000000, 4294967295))
                self.assertEqual(p["signal"], name)
                self.assertEqual(p["nominal_hz"], hz)
                self.assertEqual(p["wire_values"], [0, 4000000000, 4294967295])

    def test_ppg_channels_have_independent_continuity(self):
        tracker = Continuity()
        for signal in (3, 9, 10):
            self.assertEqual(tracker.observe(self.packet(100, signal=signal, values=(1, 2)))["state"], "baseline")
        self.assertEqual(tracker.observe(self.packet(104, signal=9, values=(1, 2)))["missing"], 2)
        for signal in (3, 10):
            self.assertEqual(tracker.observe(self.packet(102, signal=signal, values=(1, 2)))["state"], "continuous")

    def test_scalar_and_unknown_signal_validation(self):
        for signal in (5, 6, 7, 8):
            with self.assertRaises(ValueError):
                self.packet(signal=signal, values=(1, 2))
        with self.assertRaises(ValueError):
            self.packet(signal=18, values=(1,))

    def test_imu_signed_axes_and_commands(self):
        for signal in range(12, 18):
            packet = self.packet(signal=signal, values=(-32768, 0, 32767))
            self.assertEqual(packet['values'], [-32768, 0, 32767])
            self.assertEqual(packet['nominal_hz'], 25)
        self.assertEqual(command('continuous', 'accel', 2), bytes([1, 4, 5, 2]))
        self.assertEqual(command('stop', 'gyro', 3), bytes([1, 2, 6, 3]))

    def test_ppg_rejects_signed_flag(self):
        for signal in (9, 10, 11):
            with self.assertRaises(ValueError):
                decode_data(struct.pack("<4BIIII", 1, signal, 3, 1, 42, 0, 0, 100))


if __name__ == "__main__": unittest.main()
