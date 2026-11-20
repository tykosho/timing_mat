"""Background loops survive a failing tick, and a stalled lap engine is visible (issue #83)."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("MOCK_HARDWARE", "true")
os.environ.setdefault("SEED_DEMO", "false")

from fastapi.testclient import TestClient  # noqa: E402

from core import clock, lap_engine as lap_engine_module  # noqa: E402
from core.db import Database  # noqa: E402
from core.lap_engine import FLUSH_STALE_SECONDS, LapEngine  # noqa: E402
from core.models import TagRead  # noqa: E402
from web.reader_health import ReaderHealth  # noqa: E402

EPC = "E28011700000020101010001"


class FakeMonotonic:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


async def wait_for(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the flush loop")
        await asyncio.sleep(0.01)


class FlushLoopSurvivesErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(os.path.join(self.tmp.name, "race.db"))
        self.db.add_participant(1, "Pat", EPC)
        self.db.update_event_settings(min_lap_seconds=45.0)
        self.now = clock.now()
        self.db.start_event(at=self.now - 1000)
        self.engine = LapEngine(db=self.db, crossing_window=0.15)
        self.engine.refresh_event()
        patcher = mock.patch.object(lap_engine_module, "FLUSH_INTERVAL", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, ago):
        self.engine.process_tag_read(TagRead(epc=EPC, timestamp=self.now - ago, rssi=-50.0))

    def test_one_failed_write_does_not_stop_lap_counting(self):
        real_insert = self.db.insert_raw_reads
        calls = []

        def flaky_insert(batch):
            calls.append(len(batch))
            if len(calls) == 1:
                raise sqlite3.OperationalError("disk I/O error")
            real_insert(batch)

        async def scenario():
            with mock.patch.object(self.db, "insert_raw_reads", side_effect=flaky_insert):
                task = asyncio.create_task(self.engine.flush_loop())
                try:
                    self.read(ago=100)                      # chip start; its raw write fails once
                    await wait_for(lambda: self.engine.flush_errors >= 1)
                    await wait_for(lambda: self.db.get_participant_by_bib(1)["start_time"] is not None)
                    self.read(ago=40)                       # 60 s later: a lap
                    await wait_for(lambda: len(self.db.get_laps_for_runner(1)) == 1)
                    await wait_for(lambda: self.db.count_raw_reads() == 2)
                    self.assertFalse(task.done(), "flush loop must keep running")
                finally:
                    task.cancel()

        with self.assertLogs("LapEngine", level="ERROR") as logs:
            asyncio.run(scenario())
        self.assertIn("disk I/O error", "\n".join(logs.output))
        self.assertEqual(self.engine.flush_errors, 1)
        self.assertIsNone(self.engine.last_flush_error, "cleared by the next good tick")
        self.assertTrue(self.engine.flush_health()["ok"])


class FlushHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = LapEngine(db=Database(os.path.join(self.tmp.name, "race.db")))
        self.mono = FakeMonotonic()
        self.engine._monotonic = self.mono

    def test_not_started_counts_as_ok(self):
        self.assertTrue(self.engine.flush_health()["ok"])

    def test_stale_once_no_tick_for_the_threshold(self):
        self.engine.flush_started_at = self.mono.t
        self.engine.last_flush_at = self.mono.t
        self.mono.t += FLUSH_STALE_SECONDS - 1
        self.assertTrue(self.engine.flush_health()["ok"])
        self.mono.t += 2
        h = self.engine.flush_health()
        self.assertFalse(h["ok"])
        self.assertEqual(h["seconds_since_tick"], FLUSH_STALE_SECONDS + 1)

    def test_never_ticked_is_stale_after_start(self):
        self.engine.flush_started_at = self.mono.t
        self.mono.t += FLUSH_STALE_SECONDS + 5
        self.assertFalse(self.engine.flush_health()["ok"])

    def test_admin_strip_turns_red(self):
        health = ReaderHealth(clock=FakeMonotonic())
        reader = {"kind": "simulator", "running": True, "connected": True}
        self.assertEqual(health.snapshot(reader, engine={"ok": True})["state"], "ok")
        h = health.snapshot(reader, engine={"ok": False, "seconds_since_tick": 42.0,
                                            "last_error": "OperationalError: disk I/O error"})
        self.assertEqual(h["state"], "stalled")
        self.assertIn("LAP ENGINE STALLED for 42s", h["summary"])
        self.assertIn("disk I/O error", h["summary"])


class AppLoopsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import app as web_app
        cls.app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = mock.patch.object(web_app, "db", Database(os.path.join(cls.tmp.name, "loops.db")))
        cls.db_patch.start()
        cls.client = TestClient(web_app.app)  # no lifespan

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def test_api_event_fails_while_the_engine_is_stalled(self):
        """The Docker HEALTHCHECK GETs /api/event."""
        self.assertEqual(self.client.get("/api/event").status_code, 200)
        stalled = {"ok": False, "running": True, "seconds_since_tick": 30.0, "errors": 3, "last_error": "x"}
        with mock.patch.object(self.app.lap_engine, "flush_health", return_value=stalled):
            res = self.client.get("/api/event")
            with mock.patch.object(self.app.admin_auth, "pin", None):
                strip = self.client.get("/api/reader/health").json()
        self.assertEqual(res.status_code, 503)
        self.assertFalse(res.json()["engine_ok"])
        self.assertEqual(res.json()["status"], "setup")  # still a full answer for anyone reading it
        self.assertNotIn("last_error", res.json())       # public endpoint: no internals
        self.assertEqual(strip["state"], "stalled")

    def test_push_loops_survive_a_failing_tick(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")

        async def scenario():
            task = asyncio.create_task(self.app._every(0.01, "test loop", flaky))
            await wait_for(lambda: len(calls) >= 3)
            self.assertFalse(task.done())
            task.cancel()

        with self.assertLogs("RaceApp", level="ERROR"):
            asyncio.run(scenario())

    def test_every_app_loop_uses_the_guard(self):
        for name in ("tag_event_loop", "reader_health_loop", "sim_broadcast_loop"):
            with mock.patch.object(self.app, "_every", new=mock.AsyncMock()) as every:
                asyncio.run(getattr(self.app, name)())
            every.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
