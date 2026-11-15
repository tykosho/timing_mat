"""Raw R200 traffic capture from inside the server (issue #51)."""
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

from hardware.frame_capture import FrameCapture, iter_capture, load_rx_bytes, open_capture
from hardware.r200_protocol import READ_MULTI_COMMAND, READ_STOP_COMMAND, extract_frames, parse_tag_frame
from hardware.serial_reader import SerialReader
from tests.test_serial_reader import EPC, FakeClock, FakeSerial, make_reader, tag_frame

WALL = lambda: datetime(2026, 11, 15, 20, 31, 1, 682000, tzinfo=timezone.utc)  # noqa: E731


class CaptureFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "sub", "capture.log")
        self.clock = FakeClock(5321.104)

    def capture(self, *args, **kw):
        cap = FrameCapture(*args, **kw)
        self.addCleanup(cap.close)
        return cap

    def read(self):
        with open(self.path) as f:
            return f.read().splitlines()

    def test_off_without_a_path(self):
        self.assertIsNone(open_capture(None))
        self.assertIsNone(open_capture(""))

    def test_line_format(self):
        cap = self.capture(self.path, clock=self.clock, wall_clock=WALL)
        cap.tx(READ_MULTI_COMMAND)
        cap.rx(b"\xaa\x02")
        lines = self.read()
        self.assertEqual(lines[0], "2026-11-15T20:31:01.682Z 5321.104 # capture started")
        self.assertEqual(lines[1], "2026-11-15T20:31:01.682Z 5321.104 tx AA0027000322FFFF4ADD")
        self.assertEqual(lines[2], "2026-11-15T20:31:01.682Z 5321.104 rx AA02")

    def test_rx_bytes_replay_through_the_parser(self):
        frame = tag_frame()
        cap = self.capture(self.path, clock=self.clock, wall_clock=WALL)
        cap.rx(frame[:4])
        cap.tx(READ_STOP_COMMAND)  # tx lines are not part of the received stream
        cap.rx(frame[4:])
        data = load_rx_bytes(self.path)
        self.assertEqual(data, frame)
        frames, _ = extract_frames(bytearray(data))
        self.assertEqual(parse_tag_frame(frames[0])[0], EPC)

    def test_appends_across_restarts(self):
        self.capture(self.path, clock=self.clock, wall_clock=WALL).rx(b"\x01")
        self.capture(self.path, clock=self.clock, wall_clock=WALL).rx(b"\x02")
        self.assertEqual(load_rx_bytes(self.path), b"\x01\x02")

    def test_size_cap_stops_capture(self):
        cap = self.capture(self.path, max_bytes=200, clock=self.clock, wall_clock=WALL)
        for _ in range(20):
            cap.rx(b"\xaa" * 8)
        self.assertFalse(cap.enabled)
        lines = self.read()
        self.assertTrue(lines[-1].endswith("# capture stopped: size cap reached"))
        self.assertLess(os.path.getsize(self.path), 200 + 100)

    def test_unopenable_path_disables_without_raising(self):
        blocker = os.path.join(self.dir.name, "file")
        open(blocker, "w").close()
        with self.assertLogs("FrameCapture", level="WARNING"):
            cap = self.capture(os.path.join(blocker, "capture.log"))
        self.assertFalse(cap.enabled)
        cap.rx(b"\x00")  # no-op, no error

    def test_iter_skips_junk_lines(self):
        lines = ["", "garbage", "t notanumber rx AA", "t 1.5 rx AADD\n"]
        self.assertEqual(list(iter_capture(lines)), [("rx", 1.5, "AADD")])


class SerialReaderCaptureTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "capture.log")

    def capture(self, *args, **kw):
        cap = FrameCapture(*args, **kw)
        self.addCleanup(cap.close)
        return cap

    def kinds(self):
        with open(self.path) as f:
            return [(kind, text) for kind, _, text in iter_capture(f)]

    def test_watchdog_rearm_is_logged_with_reason(self):
        clock = FakeClock()
        conn = FakeSerial()
        cap = self.capture(self.path, clock=clock)
        reader = make_reader(clock, conn, rearm_interval=300, silence_rearm=30, capture=cap)
        reader._arm()
        clock.t += 31
        reader._maybe_rearm()
        self.assertEqual(self.kinds(), [
            ("#", "capture started"),
            ("#", "arm connect"), ("tx", READ_MULTI_COMMAND.hex().upper()),
            ("#", "arm silence"), ("tx", READ_STOP_COMMAND.hex().upper()),
            ("tx", READ_MULTI_COMMAND.hex().upper()),
        ])

    def test_read_loop_logs_received_chunks(self):
        frame = tag_frame()
        conn = FakeSerial(chunks=[frame[:7], frame[7:]])
        reads = []
        reader = SerialReader(on_tag_read=reads.append, serial_factory=lambda **_: conn,
                              capture=self.capture(self.path))
        reader.start()
        time.sleep(0.3)
        reader.stop()
        self.assertEqual([r.epc for r in reads], [EPC])
        self.assertEqual(load_rx_bytes(self.path), frame)
        kinds = self.kinds()
        self.assertIn(("#", "reader stopped"), kinds)
        self.assertEqual(kinds[-2], ("tx", READ_STOP_COMMAND.hex().upper()))

    def test_no_capture_by_default(self):
        reader = SerialReader(serial_factory=lambda **_: FakeSerial())
        self.assertIsNone(reader.capture)


if __name__ == "__main__":
    unittest.main()
