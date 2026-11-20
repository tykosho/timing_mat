"""Race clock: lap times must not change when the Pi's system clock jumps (issue #2)."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from core import clock as clock_module
from core.clock import RaceClock
from core.db import Database
from core.lap_engine import LapEngine
from core.models import TagRead

EPC = "E28011700000020101010001"


class FakeTime:
    """A wall clock that can jump, and a monotonic clock that only moves forward."""
    def __init__(self, wall=1_700_000_000.0):
        self.wall_t = wall
        self.mono_t = 50.0

    def wall(self):
        return self.wall_t

    def mono(self):
        return self.mono_t

    def advance(self, seconds):
        self.wall_t += seconds
        self.mono_t += seconds

    def jump_wall(self, seconds):
        self.wall_t += seconds  # NTP sync, someone running `date -s`, ...


class RaceClockTest(unittest.TestCase):
    def setUp(self):
        self.t = FakeTime()
        self.clock = RaceClock(wall=self.t.wall, mono=self.t.mono)

    def test_starts_at_wall_time_and_follows_monotonic(self):
        self.assertEqual(self.clock.now(), 1_700_000_000.0)
        self.t.advance(12.5)
        self.assertEqual(self.clock.now(), 1_700_000_012.5)

    def test_wall_jump_does_not_move_race_clock(self):
        before = self.clock.now()
        self.t.jump_wall(3600)
        self.assertEqual(self.clock.now(), before)
        self.assertEqual(self.clock.drift(), 3600)
        self.t.jump_wall(-7200)
        self.assertEqual(self.clock.now(), before)

    def test_ensure_not_before_only_moves_forward(self):
        now = self.clock.now()
        self.assertFalse(self.clock.ensure_not_before(None))
        self.assertFalse(self.clock.ensure_not_before(now - 100))
        self.assertEqual(self.clock.now(), now)
        with self.assertLogs("RaceClock", level="WARNING"):
            self.assertTrue(self.clock.ensure_not_before(now + 5000))
        self.assertGreater(self.clock.now(), now + 5000)

    def test_maintain_ignores_small_drift(self):
        self.t.jump_wall(1.0)
        self.assertIsNone(self.clock.maintain(race_in_progress=False))

    def test_maintain_during_race_warns_once_and_keeps_counting(self):
        before = self.clock.now()
        self.t.jump_wall(3600)
        with self.assertLogs("RaceClock", level="WARNING"):
            self.assertEqual(self.clock.maintain(race_in_progress=True), "warned")
        self.assertIsNone(self.clock.maintain(race_in_progress=True))  # no repeat
        self.assertEqual(self.clock.now(), before)

    def test_maintain_between_races_resyncs(self):
        self.t.jump_wall(3600)  # e.g. the Pi finally got NTP
        self.assertEqual(self.clock.maintain(race_in_progress=False), "resynced")
        self.assertEqual(self.clock.now(), self.t.wall())

    def test_resync_never_goes_before_stored_times(self):
        latest = self.clock.now()
        self.t.jump_wall(-3600)
        self.clock.maintain(race_in_progress=False, latest_timestamp=latest)
        self.assertGreaterEqual(self.clock.now(), latest)


class LapTimesSurviveClockJumpsTest(unittest.TestCase):
    """The issue's 'done when': ±1 h system clock changes mid-race leave lap durations alone."""

    def setUp(self):
        self.t = FakeTime()
        patcher = mock.patch.object(clock_module, "clock", RaceClock(wall=self.t.wall, mono=self.t.mono))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(os.path.join(self.tmp.name, "race.db"))
        self.db.add_participant(101, "Alex", EPC)
        self.db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=600.0)
        self.db.start_event()
        self.engine = LapEngine(db=self.db, crossing_window=0.5)

    def cross(self):
        """A burst of reads as the runner crosses, timestamped like the real reader does."""
        reads = []
        for _ in range(3):
            reads.append(TagRead(epc=EPC, timestamp=clock_module.now(), rssi=-50.0))
            self.t.advance(0.1)
        return asyncio.run(self.engine._resolve_crossing(EPC, [(r.timestamp, r.rssi) for r in reads]))

    def test_lap_durations_ignore_wall_clock_jumps(self):
        burst = 0.3  # each crossing's reads span 0.3 s, so peaks are that much further apart
        self.t.advance(30)
        self.assertEqual(self.cross(), "started")
        self.t.advance(70)
        self.t.jump_wall(+3600)  # NTP jumps an hour ahead mid-lap
        self.assertEqual(self.cross(), "lap")
        self.t.advance(80)
        self.t.jump_wall(-7200)  # ...and back two hours
        self.assertEqual(self.cross(), "lap")
        durations = [lap["lap_duration_seconds"] for lap in self.db.get_laps_for_runner(101)]
        self.assertEqual(durations, [70 + burst, 80 + burst])

    def test_maintain_during_race_keeps_laps_consistent(self):
        self.t.advance(30)
        self.cross()
        self.t.jump_wall(3600)
        self.assertTrue(self.db.race_in_progress())
        self.engine.maintain_clock()  # sees the jump, must not re-anchor
        self.t.advance(65)
        self.cross()
        self.assertEqual(self.db.get_laps_for_runner(101)[0]["lap_duration_seconds"], 65.3)  # + 0.3 s burst

    def test_latest_timestamp_and_race_in_progress(self):
        self.t.advance(30)
        self.cross()
        self.t.advance(70)
        self.cross()
        last_lap = self.db.get_laps_for_runner(101)[-1]["crossing_time"]
        self.assertGreaterEqual(self.db.latest_timestamp(), last_lap)
        self.db.end_event()
        self.assertFalse(self.db.race_in_progress())

    def test_latest_timestamp_empty_db(self):
        db = Database(os.path.join(self.tmp.name, "empty.db"))
        latest = db.latest_timestamp()  # only the default event's created_at
        self.assertTrue(latest is None or latest <= clock_module.now())


class PageClockOffsetTest(unittest.TestCase):
    def test_pages_get_the_race_clock(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:
            self.skipTest(str(e))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ.setdefault("DB_PATH", os.path.join(tmp.name, "clock_import.db"))
        os.environ.setdefault("MOCK_HARDWARE", "true")
        from web import app as web_app
        # web.app is shared across test modules; use our own DB (test_api.py deletes its one)
        db = Database(os.path.join(tmp.name, "clock_page.db"))
        t = FakeTime(wall=1_234_567_890.0)
        with mock.patch.object(clock_module, "clock", RaceClock(wall=t.wall, mono=t.mono)), \
                mock.patch.object(web_app, "db", db):
            html = TestClient(web_app.app).get("/leaderboard").text
        self.assertIn("let clockOffsetSec = 1234567890.000 - Date.now() / 1000;", html)
        self.assertIn("raceNowSec() - eventState.start_time", html)


if __name__ == "__main__":
    unittest.main()
