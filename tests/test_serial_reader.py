"""SerialReader against a fake serial port: inventory start, watchdog re-arm (issue #5), reconnect."""
import threading
import time
import unittest

from hardware.r200_protocol import READ_MULTI_COMMAND, READ_STOP_COMMAND, calculate_checksum
import serial

from hardware.serial_reader import WRITE_TIMEOUT, SerialReader

EPC = "E28011700000020101010001"


def tag_frame(epc_hex: str = EPC, raw_rssi: int = 0x45) -> bytes:
    payload = bytes([raw_rssi, 0x30, 0x00]) + bytes.fromhex(epc_hex) + b"\x12\x34"
    body = bytes([0x02, 0x22, len(payload) >> 8, len(payload) & 0xFF]) + payload
    return bytes([0xAA]) + body + bytes([calculate_checksum(body), 0xDD])


class FakeSerial:
    """Records writes; read() hands out queued chunks, then b"" (a read timeout)."""
    def __init__(self, chunks=None, fail_after_reads=None):
        self.writes = []
        self.chunks = list(chunks or [])
        self.is_open = True
        self.reads = 0
        self.fail_after_reads = fail_after_reads
        self.lock = threading.Lock()

    def write(self, data):
        with self.lock:
            self.writes.append(bytes(data))

    def flush(self):
        pass

    def read(self, n):
        self.reads += 1
        if self.fail_after_reads is not None and self.reads > self.fail_after_reads:
            raise OSError("device unplugged")
        time.sleep(0.001)
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.is_open = False


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_reader(clock, conn, reads=None, **kw):
    reader = SerialReader(on_tag_read=(reads.append if reads is not None else None),
                          serial_factory=lambda **_: conn, clock=clock, **kw)
    reader.serial_conn = conn
    return reader


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.conn = FakeSerial()
        self.reader = make_reader(self.clock, self.conn, rearm_interval=300, silence_rearm=30)
        self.reader._arm()
        self.conn.writes.clear()

    def test_nothing_due_right_after_arming(self):
        self.clock.t += 10
        self.assertIsNone(self.reader._maybe_rearm())
        self.assertEqual(self.conn.writes, [])

    def test_rearms_after_silence_with_stop_then_start(self):
        self.clock.t += 31
        self.assertEqual(self.reader._maybe_rearm(), "silence")
        self.assertEqual(self.conn.writes, [READ_STOP_COMMAND, READ_MULTI_COMMAND])
        self.assertEqual(self.reader.rearm_count, 1)
        # Silence is counted again from the re-arm, not from the last frame
        self.clock.t += 10
        self.assertIsNone(self.reader._maybe_rearm())

    def test_frames_keep_the_silence_watchdog_quiet(self):
        for _ in range(10):
            self.clock.t += 20
            self.reader._process_stream(bytearray(tag_frame()))
            self.assertIsNone(self.reader._maybe_rearm())

    def test_routine_rearm_even_while_tags_are_read(self):
        """The 65,535-poll limit runs out however busy the field is, so re-arm on a timer too."""
        while self.clock.t + 5 < 1000 + 300:  # up to 295 s after arming
            self.clock.t += 5
            self.reader._process_stream(bytearray(tag_frame()))
            self.assertIsNone(self.reader._maybe_rearm())
        self.clock.t += 5
        self.assertEqual(self.reader._maybe_rearm(), "routine")
        self.assertEqual(self.conn.writes, [READ_STOP_COMMAND, READ_MULTI_COMMAND])

    def test_non_tag_frames_count_as_life(self):
        # e.g. an error/"no tag" response frame: the module is clearly still talking
        response = bytes([0xAA, 0x01, 0x27, 0x00, 0x01, 0x00, 0x29, 0xDD])
        self.clock.t += 25
        self.reader._process_stream(bytearray(response))
        self.clock.t += 25
        self.assertIsNone(self.reader._maybe_rearm())

    def test_zero_turns_a_trigger_off(self):
        reader = make_reader(self.clock, self.conn, rearm_interval=0, silence_rearm=0)
        reader._arm()
        self.clock.t += 10_000
        self.assertIsNone(reader._maybe_rearm())

    def test_silence_logged_once_per_quiet_spell(self):
        with self.assertLogs("SerialReader", level="INFO") as logs:
            self.clock.t += 31
            self.reader._maybe_rearm()
            self.clock.t += 31
            self.reader._maybe_rearm()
        self.assertEqual(len([m for m in logs.output if "No frames" in m]), 1)
        self.assertEqual(self.reader.rearm_count, 2)


class ReadLoopTest(unittest.TestCase):
    """Runs the real thread loop briefly against the fake port."""

    def run_loop(self, reader, seconds=0.3):
        reader.start()
        time.sleep(seconds)
        reader.stop()

    def test_connect_arms_and_emits_tag_reads(self):
        frame = tag_frame()
        conn = FakeSerial(chunks=[frame[:7], frame[7:] + tag_frame("E28011700000020101010002")])
        reads = []
        reader = SerialReader(on_tag_read=reads.append, serial_factory=lambda **_: conn)
        self.run_loop(reader)
        self.assertEqual(conn.writes[0], READ_MULTI_COMMAND)
        self.assertEqual(conn.writes[-1], READ_STOP_COMMAND)  # stop() ends the inventory
        self.assertEqual([r.epc for r in reads], [EPC, "E28011700000020101010002"])

    def test_watchdog_runs_inside_the_loop(self):
        conn = FakeSerial()
        reader = SerialReader(serial_factory=lambda **_: conn, silence_rearm=0.05, rearm_interval=0)
        self.run_loop(reader, seconds=0.4)
        self.assertGreaterEqual(reader.rearm_count, 2)
        self.assertGreaterEqual(conn.writes.count(READ_MULTI_COMMAND), 3)  # connect + re-arms

    def test_reconnects_and_rearms_after_read_error(self):
        conns = [FakeSerial(fail_after_reads=3), FakeSerial()]
        made = []

        def factory(**_):
            made.append(conns[len(made)] if len(made) < len(conns) else FakeSerial())
            return made[-1]

        reader = SerialReader(serial_factory=factory)
        self.run_loop(reader, seconds=1.4)  # read error -> 1 s back-off -> reconnect
        self.assertGreaterEqual(len(made), 2)
        self.assertFalse(conns[0].is_open)
        self.assertEqual(conns[1].writes[0], READ_MULTI_COMMAND)


class WriteTimeoutTest(unittest.TestCase):
    """A wedged USB-UART adapter must not hang the reader thread in write() (issue #89)."""

    def test_port_is_opened_with_a_write_timeout(self):
        opened = []

        def factory(**kw):
            opened.append(kw)
            return FakeSerial()

        reader = SerialReader(serial_factory=factory)
        ReadLoopTest.run_loop(self, reader, seconds=0.1)
        self.assertEqual(opened[0]["write_timeout"], WRITE_TIMEOUT)
        self.assertLessEqual(WRITE_TIMEOUT, 2.0)

    def test_stuck_rearm_write_reconnects(self):
        class Wedged(FakeSerial):
            def write(self, data):
                if self.writes:   # the connect arm works; the next write times out
                    raise serial.SerialTimeoutException("Write timeout")
                super().write(data)

        conns = [Wedged(), FakeSerial()]
        made = []

        def factory(**_):
            made.append(conns[len(made)] if len(made) < len(conns) else FakeSerial())
            return made[-1]

        reader = SerialReader(serial_factory=factory, silence_rearm=0.05, rearm_interval=0)
        ReadLoopTest.run_loop(self, reader, seconds=1.4)
        self.assertGreaterEqual(len(made), 2)
        self.assertFalse(conns[0].is_open)
        self.assertEqual(conns[1].writes[0], READ_MULTI_COMMAND)

    def test_port_closed_when_arming_fails_right_after_open(self):
        class CantArm(FakeSerial):
            def write(self, data):
                raise serial.SerialTimeoutException("Write timeout")

        made = []

        def factory(**_):
            made.append(CantArm())
            return made[-1]

        reader = SerialReader(serial_factory=factory)
        ReadLoopTest.run_loop(self, reader, seconds=0.1)
        self.assertEqual(len(made), 1)
        self.assertFalse(made[0].is_open)
        self.assertFalse(reader.connected)
        self.assertIn("Write timeout", reader.last_error)


if __name__ == "__main__":
    unittest.main()
