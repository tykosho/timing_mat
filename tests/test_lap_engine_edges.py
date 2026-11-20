"""Lap engine edge cases (issue #11): paused time, no 30 s floor, tags parked by the mat."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from core.db import Database
from core.lap_engine import LapEngine, MAX_BURST_SECONDS
from core.models import TagRead

EPC = "E28011700000020101010001"


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(os.path.join(self.tmp.name, "race.db"))
        self.db.add_participant(101, "Alex", EPC)
        self.db.update_event_settings(min_lap_seconds=20.0, max_lap_rest_seconds=600.0)
        self.engine = LapEngine(db=self.db, crossing_window=1.0)

    def at(self, t):
        """Pretend the race clock reads `t` (for pause/start/end, which stamp their own time)."""
        return mock.patch("core.clock.now", return_value=t)

    def cross(self, t):
        return asyncio.run(self.engine._resolve_crossing(EPC, [(t, -50.0)]))

    def durations(self):
        return [lap["lap_duration_seconds"] for lap in self.db.get_laps_for_runner(101)]


class PauseTest(EngineTestCase):
    def test_paused_time_is_not_part_of_the_lap(self):
        self.db.start_event(at=1000.0)
        self.assertEqual(self.cross(1100.0), "started")
        with self.at(1150.0):
            self.db.pause_event()
        self.assertIsNone(self.cross(1300.0), "crossings aren't timed while paused")
        self.db.start_event(at=1450.0)          # 300 s pause
        self.assertEqual(self.cross(1500.0), "lap")
        # 400 s on the wall clock, minus the 300 s pause
        self.assertEqual(self.durations(), [100.0])

    def test_pauses_add_up(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        for pause_start, resume in [(1010.0, 1060.0), (1070.0, 1100.0)]:  # 50 s + 30 s
            with self.at(pause_start):
                self.db.pause_event()
            self.db.start_event(at=resume)
        self.cross(1180.0)
        self.assertEqual(self.durations(), [100.0])

    def test_reopened_event_skips_the_time_it_was_ended(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        self.cross(1060.0)
        with self.at(1100.0):
            self.db.end_event()
        self.db.reopen_event()                  # End pressed by mistake
        self.db.start_event(at=1700.0)          # 600 s later
        self.cross(1760.0)
        # 1760 - 1060 = 700 s on the wall clock, 600 of them ended
        self.assertEqual(self.durations(), [60.0, 100.0])

    def test_resume_without_pause_changes_nothing(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        self.db.start_event(at=1050.0)          # already running: no-op
        self.cross(1070.0)
        self.assertEqual(self.durations(), [70.0])

    def test_pause_only_affects_runners_who_started(self):
        self.db.add_participant(102, "Sam", "E28011700000020101010002")
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        with self.at(1010.0):
            self.db.pause_event()
        self.db.start_event(at=1100.0)
        self.assertIsNone(self.db.get_participant_by_bib(102)["last_crossing_time"])
        self.assertEqual(self.db.get_participant_by_bib(101)["last_crossing_time"], 1090.0)

    def test_existing_database_gets_the_new_column(self):
        path = os.path.join(self.tmp.name, "old.db")
        Database(path)
        with sqlite3.connect(path) as conn:      # a race DB from before this change
            conn.execute("ALTER TABLE events DROP COLUMN paused_at;")
        db = Database(path)
        db.start_event(at=1.0)
        with self.at(2.0):
            self.assertEqual(db.pause_event()["paused_at"], 2.0)


class NoLapFloorTest(EngineTestCase):
    def test_short_lap_is_stored_as_measured(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        self.assertEqual(self.cross(1025.0), "lap")  # 25 s: above the 20 s minimum
        self.assertEqual(self.durations(), [25.0])    # was stored as 30.0

    def test_below_minimum_is_still_rejected(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        self.assertEqual(self.cross(1015.0), "debounced")
        self.assertEqual(self.durations(), [])


class ParkedTagTest(EngineTestCase):
    def feed(self, start, seconds, every=0.5):
        """A tag sitting in the antenna field, read every `every` seconds, flushing as we go."""
        t = start
        outcomes = []
        while t < start + seconds:
            self.engine.process_tag_read(TagRead(epc=EPC, timestamp=t, rssi=-50.0))
            asyncio.run(self.engine.flush_once(t))
            outcomes.append(len(self.engine.buffers.get(EPC, [])))
            t += every
        return outcomes

    def too_soon_events(self):
        events = []
        self.engine.on_crossing_too_soon = events.append
        return events

    def leave(self, t):
        """The tag leaves the field after its last read at `t`."""
        asyncio.run(self.engine.flush_once(t + 5.0))

    def test_parked_tag_is_resolved_and_buffer_stays_small(self):
        self.db.start_event(at=1000.0)
        too_soon = self.too_soon_events()
        sizes = self.feed(1000.0, 60.0)
        # Resolved after ~MAX_BURST_SECONDS instead of never; the buffer never grows past one burst
        self.assertEqual(self.db.get_participant_by_bib(101)["start_time"], 1000.0)
        self.assertLessEqual(max(sizes), MAX_BURST_SECONDS / 0.5 + 1)
        self.leave(1060.0)
        # Issue #85: 60 s parked is one visit to the mat, not a start plus laps every 8 s
        self.assertEqual(self.durations(), [])
        self.assertEqual(too_soon, [])

    def test_lingering_after_a_lap_adds_no_phantom_laps(self):
        # The repro from issue #85: min lap 45 s, a tag left on the mat for 180 s after a real lap
        self.db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=600.0)
        self.db.start_event(at=1000.0)
        too_soon = self.too_soon_events()
        self.cross(1000.0)
        self.feed(1100.0, 180.0)
        self.leave(1280.0)
        self.assertEqual(self.durations(), [100.0])
        self.assertEqual(too_soon, [])
        # Once it has left the field, the next crossing is an ordinary lap again
        self.feed(1400.0, 1.0)
        self.leave(1401.0)
        self.assertEqual(self.durations(), [100.0, 300.0])

    def test_tag_leaving_right_after_a_forced_resolve_is_forgotten(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        # Parked exactly long enough to be force-resolved, then gone with nothing left buffered
        self.feed(1050.0, MAX_BURST_SECONDS + 0.5)
        self.leave(1050.0 + MAX_BURST_SECONDS)
        self.assertEqual(self.durations(), [50.0])
        self.feed(1100.0, 1.0)
        self.leave(1101.0)
        self.assertEqual(self.durations(), [50.0, 50.0])

    def test_runner_waiting_on_the_mat_before_the_gun_still_starts(self):
        self.feed(990.0, 10.0)          # standing on the mat, event not started yet
        self.db.start_event(at=1000.0)
        self.feed(1000.0, 20.0)
        self.leave(1020.0)
        start = self.db.get_participant_by_bib(101)["start_time"]
        self.assertIsNotNone(start)
        self.assertLessEqual(start, 1000.0 + MAX_BURST_SECONDS)
        self.assertEqual(self.durations(), [])

    def test_normal_crossing_still_resolves_on_the_quiet_gap(self):
        self.db.start_event(at=1000.0)
        for t in (1000.0, 1000.2, 1000.4):
            self.engine.process_tag_read(TagRead(epc=EPC, timestamp=t, rssi=-50.0))
        asyncio.run(self.engine.flush_once(1001.0))
        self.assertIn(EPC, self.engine.buffers, "0.6 s after the last read: still in the field")
        asyncio.run(self.engine.flush_once(1001.5))
        self.assertNotIn(EPC, self.engine.buffers)
        self.assertIsNotNone(self.db.get_participant_by_bib(101)["start_time"])


class AutoBreakDisabledTest(EngineTestCase):
    def test_disabled_auto_break_records_slow_crossing_as_lap_not_break(self):
        # Disable auto-break by setting max_lap_rest_seconds to 0
        self.db.update_event_settings(min_lap_seconds=20.0, max_lap_rest_seconds=0.0)
        self.db.start_event(at=1000.0)
        self.assertEqual(self.cross(1000.0), "started")

        # Cross 1500 seconds later (25 minutes)
        outcome = self.cross(2500.0)
        self.assertEqual(outcome, "lap", "Should record as a lap when auto-break is disabled")
        self.assertEqual(self.durations(), [1500.0])
        self.assertEqual(len(self.db.get_breaks_for_runner(101)), 0, "No breaks should be logged")

    def test_leaderboard_does_not_mark_runner_on_break_when_disabled(self):
        self.db.update_event_settings(min_lap_seconds=20.0, max_lap_rest_seconds=0.0)
        self.db.start_event(at=1000.0)
        self.cross(1000.0)

        # 30 minutes later
        with self.at(2800.0):
            lb = self.db.get_leaderboard()
            runner = next(r for r in lb if r["bib_number"] == 101)
            self.assertEqual(runner["status"], "active")


if __name__ == "__main__":
    unittest.main()
