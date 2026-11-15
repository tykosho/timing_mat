"""Reader health strip on the admin page (issue #48)."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from hardware.serial_reader import SerialReader
from web.reader_health import QUIET_AFTER_SECONDS, ReaderHealth


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class ReaderHealthTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.health = ReaderHealth(clock=self.clock)

    def serial(self, **kw):
        base = {"kind": "serial", "port": "/dev/ttyUSB0", "running": True, "connected": True,
                "seconds_since_frame": 0.5, "reconnects": 0, "rearms": 0, "last_error": None}
        base.update(kw)
        return base

    def test_reads_per_second_and_last_tag(self):
        for _ in range(30):
            self.health.note_read()
        self.clock.t += 2.0
        h = self.health.snapshot(self.serial())
        self.assertEqual(h["reads_per_second"], 15.0)
        self.assertEqual(h["total_reads"], 30)
        self.assertEqual(h["seconds_since_read"], 2.0)
        self.assertEqual(h["state"], "ok")
        self.assertEqual(h["summary"], "Reader on /dev/ttyUSB0: reading · 15 reads/s · last tag 2s ago")

    def test_rate_drops_to_zero_when_reads_stop(self):
        for _ in range(10):
            self.health.note_read()
        self.clock.t += 2.0
        self.health.snapshot(self.serial())
        self.clock.t += 2.0
        self.assertEqual(self.health.snapshot(self.serial())["reads_per_second"], 0.0)

    def test_disconnected_is_red_with_the_error(self):
        h = self.health.snapshot(self.serial(connected=False, seconds_since_frame=None,
                                             last_error="Can't open /dev/ttyUSB0: no such file"))
        self.assertEqual(h["state"], "disconnected")
        self.assertEqual(h["summary"], "Reader on /dev/ttyUSB0: NOT CONNECTED (Can't open /dev/ttyUSB0: no such file)")

    def test_connected_but_silent_is_amber(self):
        h = self.health.snapshot(self.serial(seconds_since_frame=QUIET_AFTER_SECONDS + 15, rearms=3))
        self.assertEqual(h["state"], "quiet")
        self.assertIn("connected, but nothing heard for 45s", h["summary"])
        self.assertIn("re-armed 3×", h["summary"])

    def test_counts_and_never(self):
        h = self.health.snapshot(self.serial(rearms=1, reconnects=2))
        self.assertIn("last tag never", h["summary"])
        self.assertIn("re-armed 1×", h["summary"])
        self.assertIn("2 reconnects", h["summary"])

    def test_simulator_and_stopped_reader(self):
        sim = self.health.snapshot({"kind": "simulator", "running": True, "connected": True, "paused": True})
        self.assertEqual(sim["state"], "ok")
        self.assertTrue(sim["summary"].startswith("Simulator: reading"))
        self.assertIn("paused", sim["summary"])
        stopped = self.health.snapshot({"kind": "simulator", "running": False, "connected": False})
        self.assertEqual((stopped["state"], stopped["summary"]), ("disconnected", "Simulator: not running"))

    def test_note_read_is_thread_safe(self):
        threads = [threading.Thread(target=lambda: [self.health.note_read() for _ in range(1000)]) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.health.total_reads, 4000)


class FlakySerial:
    """A port that answers until `fail_after` reads, then errors like a pulled USB cable."""
    def __init__(self, fail_after=None):
        self.fail_after = fail_after
        self.reads = 0
        self.is_open = True

    def write(self, data):
        pass

    def flush(self):
        pass

    def read(self, n):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError("device disconnected")
        time.sleep(0.005)
        return bytes([0xAA, 0x01, 0x27, 0x00, 0x01, 0x00, 0x29, 0xDD])  # any frame = the module talks

    def close(self):
        self.is_open = False


class SerialReaderHealthTest(unittest.TestCase):
    def wait_for(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_unplug_turns_red_and_replug_turns_green(self):
        """The issue's done-when, with a fake port: connected -> read error -> reconnected."""
        ports = [FlakySerial(fail_after=20), FlakySerial()]
        reader = SerialReader(serial_factory=lambda **_: ports.pop(0) if ports else FlakySerial())
        health = ReaderHealth()
        reader.start()
        self.addCleanup(reader.stop)

        self.assertTrue(self.wait_for(lambda: reader.health()["connected"]))
        self.assertEqual(health.snapshot(reader.health())["state"], "ok")

        self.assertTrue(self.wait_for(lambda: not reader.health()["connected"]), "read error -> disconnected")
        h = health.snapshot(reader.health())
        self.assertEqual(h["state"], "disconnected")
        self.assertIn("device disconnected", h["summary"])

        self.assertTrue(self.wait_for(lambda: reader.health()["connected"]), "reconnects after the 1 s back-off")
        h = health.snapshot(reader.health())
        self.assertEqual(h["state"], "ok")
        self.assertEqual(h["reconnects"], 1)
        self.assertIsNone(h["last_error"])

    def test_port_that_cannot_be_opened(self):
        def factory(**_):
            raise OSError("[Errno 2] No such file or directory: '/dev/ttyUSB0'")
        reader = SerialReader(serial_factory=factory)
        reader.start()
        self.addCleanup(reader.stop)
        self.assertTrue(self.wait_for(lambda: reader.health()["last_error"] is not None))
        h = ReaderHealth().snapshot(reader.health())
        self.assertEqual(h["state"], "disconnected")
        self.assertEqual(h["summary"], "Reader on /dev/ttyUSB0: NOT CONNECTED "
                                       "(no device at /dev/ttyUSB0 (is the reader plugged in?))")

    def test_open_errors_are_explained(self):
        reader = SerialReader(port="/dev/serial/by-id/x")
        cases = [
            ("could not open port /dev/x: [Errno 13] Permission denied: '/dev/x'", "permission denied on"),
            ("could not open port /dev/x: [Errno 16] Device or resource busy", "in use by another program"),
            ("something else", "can't open /dev/serial/by-id/x: something else"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertIn(expected, reader._describe_open_error(OSError(message)))

    def test_stop_marks_disconnected(self):
        reader = SerialReader(serial_factory=lambda **_: FlakySerial())
        reader.start()
        self.assertTrue(self.wait_for(lambda: reader.health()["connected"]))
        reader.stop()
        self.assertFalse(reader.health()["connected"])


class ReaderHealthApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:
            raise unittest.SkipTest(str(e))
        os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "health_import.db"))
        os.environ.setdefault("MOCK_HARDWARE", "true")
        from core.db import Database
        from web import app as web_app
        cls.app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = mock.patch.object(web_app, "db", Database(os.path.join(cls.tmp.name, "health.db")))
        cls.db_patch.start()
        cls.client = TestClient(web_app.app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def open_auth(self):
        return mock.patch.object(self.app.admin_auth, "pin", None)

    def test_endpoint_and_admin_strip(self):
        with self.open_auth():
            h = self.client.get("/api/reader/health").json()
            page = self.client.get("/admin").text
        self.assertEqual(h["kind"], "simulator")
        self.assertIn(h["state"], ("ok", "disconnected"))
        self.assertIn('id="readerHealth"', page)
        self.assertIn(h["summary"].split(":")[0], page)

    def test_endpoint_is_admin_only(self):
        from web.auth import AdminAuth
        with mock.patch.object(self.app, "admin_auth", AdminAuth("482913")):
            self.assertEqual(self.client.get("/api/reader/health").status_code, 401)

    def test_reads_are_counted(self):
        before = self.app.reader_health.total_reads
        from core.models import TagRead
        with mock.patch.object(self.app, "main_event_loop", None):
            self.app.on_reader_tag(TagRead(epc="E1", timestamp=1.0, rssi=-50.0))
        self.assertEqual(self.app.reader_health.total_reads, before + 1)

    def test_broken_health_never_breaks_the_admin_page(self):
        broken = mock.Mock()
        broken.health.side_effect = RuntimeError("boom")
        with self.open_auth(), mock.patch.object(self.app, "reader", broken):
            h = self.client.get("/api/reader/health").json()
            page = self.client.get("/admin")
        self.assertEqual(h["state"], "disconnected")
        self.assertIn("health check failed: boom", h["summary"])
        self.assertEqual(page.status_code, 200)


if __name__ == "__main__":
    unittest.main()
