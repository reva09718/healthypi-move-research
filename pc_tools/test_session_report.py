"""Offline test for session_report.py: python -m unittest discover -s pc_tools -p test_session_report.py"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cloud_storage import AesGcmCipher
from research_storage import PacketBuilder, SessionStore
import session_report


class SessionReportTests(unittest.TestCase):
    def test_gap_and_events_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            store = SessionStore(Path(d) / "s.db", cipher=AesGcmCipher(os.urandom(32)))
            sid = store.start(["ecg"], device="AA:BB", note="")
            clock = [1000.0]
            with mock.patch("time.monotonic", lambda: clock[0]), mock.patch("time.time", lambda: clock[0]):
                b = PacketBuilder()
                store.event(sid, 0.0, "connected", "connection segment 0")
                for i in range(120):                 # 60 s at 2 packets/s, 8 samples each
                    if i == 60:
                        clock[0] += 10               # 10 s reception gap after 30 s
                        store.event(sid, clock[0] - 1000, "connection_gap", "x")
                        b.connection_segment += 1
                        store.event(sid, clock[0] - 1000, "connected", "connection segment 1")
                    store.append(sid, b.make("ecg", list(range(8)), 16))
                    clock[0] += 0.5
            store.flush(); store.finish(sid, "stopped")
            csv_path = Path(d) / "out.csv"
            store.export_csv(sid, csv_path)
            store.export_events_csv(sid, Path(d) / "out_events.csv")
            store.close()
            report = session_report.build_report(csv_path, gap_s=2)
        self.assertIn("| ecg | 960 |", report)
        self.assertIn("2 connection(s), 1 interruption(s)", report)
        self.assertIn("ecg (1): at 0:00:30 for 10.5 s", report)


if __name__ == "__main__":
    unittest.main()
