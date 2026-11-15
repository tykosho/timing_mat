"""Reads from the reader thread reach the lap engine on the event loop only (issue #13)."""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest import mock

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("DB_PATH", os.path.join(_tmp.name, "handoff_import.db"))
os.environ.setdefault("MOCK_HARDWARE", "true")
os.environ.setdefault("SEED_DEMO", "false")

from core.models import TagRead  # noqa: E402

try:
    from web import app as web_app
except ImportError:  # fastapi missing
    web_app = None


@unittest.skipIf(web_app is None, "web app unavailable")
class ReaderHandoffTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.addCleanup(self._stop_loop)
        self.seen = []
        self.patches = [mock.patch.object(web_app, "main_event_loop", self.loop),
                        mock.patch.object(web_app.lap_engine, "process_tag_read",
                                          lambda r: self.seen.append((threading.get_ident(), r)))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _stop_loop(self):
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.loop_thread.join(timeout=2)
            self.loop.close()

    def drain(self):
        """Waits until everything queued on the loop so far has run."""
        done = threading.Event()
        self.loop.call_soon_threadsafe(done.set)
        self.assertTrue(done.wait(timeout=5))

    def test_every_read_runs_on_the_loop_thread(self):
        def reader_thread(n):
            for i in range(500):
                web_app.on_reader_tag(TagRead(epc=f"E{n:02d}", timestamp=float(i), rssi=-50.0))
        threads = [threading.Thread(target=reader_thread, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.drain()
        self.assertEqual(len(self.seen), 2000, "no read lost")
        self.assertEqual({ident for ident, _ in self.seen}, {self.loop_thread.ident})
        # Per reader thread, reads keep their order
        first_reader = [r.timestamp for _, r in self.seen if r.epc == "E00"]
        self.assertEqual(first_reader, sorted(first_reader))

    def test_no_loop_yet_is_ignored(self):
        with mock.patch.object(web_app, "main_event_loop", None):
            web_app.on_reader_tag(TagRead(epc="E", timestamp=1.0, rssi=-50.0))
        self.drain()
        self.assertEqual(self.seen, [])

    def test_closed_loop_during_shutdown_is_ignored(self):
        self._stop_loop()
        web_app.on_reader_tag(TagRead(epc="E", timestamp=1.0, rssi=-50.0))  # must not raise
        self.assertEqual(self.seen, [])

    def test_reader_is_wired_through_the_handoff(self):
        self.assertIs(web_app.reader.on_tag_read, web_app.on_reader_tag)


if __name__ == "__main__":
    unittest.main()
