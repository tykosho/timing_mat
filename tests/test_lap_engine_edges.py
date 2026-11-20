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

    def test_crossing_just_before_pause_still_counts(self):
        # Issue #91: the burst is only resolved ~2 s after the last read, by when Pause may
        # already have been pressed. Like a crossing just before End, it still counts.
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        with self.at(1101.5):
            self.db.pause_event()
        self.assertEqual(self.cross(1100.0), "lap")
        self.assertIsNone(self.cross(1102.0), "a crossing after Pause still isn't timed")
        self.db.start_event(at=1201.5)          # 100 s pause
        self.cross(1260.0)
        self.assertEqual(self.durations(), [100.0, 60.0])

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

    def test_lingering_tag_that_left_and_came_back_before_a_flush_counts(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        self.feed(1050.0, MAX_BURST_SECONDS + 0.5)       # parked, force-resolved as a lap
        # Gone for 60 s, then back across the mat with no flush in between (e.g. a stall)
        for t in (1110.0, 1110.2):
            self.engine.process_tag_read(TagRead(epc=EPC, timestamp=t, rssi=-50.0))
        self.leave(1110.2)
        self.assertEqual(self.durations(), [50.0, 60.0])

    def test_reset_forgets_parked_tags(self):
        self.db.start_event(at=1000.0)
        self.engine.refresh_event()
        self.feed(1000.0, 10.0)
        self.engine.refresh_event()
        self.assertIn(EPC, self.engine.lingering)
        with self.at(1010.0):
            self.db.pause_event()
        self.db.reset_race_data()
        self.engine.refresh_event()
        self.assertEqual(self.engine.lingering, {})

    def test_reset_start_while_the_tag_is_on_the_mat_starts_it_again(self):
        """#113: Reset used to wait for a parked tag to leave the field and come back."""
        self.db.start_event(at=1000.0)
        self.feed(1000.0, MAX_BURST_SECONDS + 2.0)              # parked: started, now lingering
        self.assertIn(EPC, self.engine.lingering)
        reset_at = 1000.0 + MAX_BURST_SECONDS + 2.0
        self.db.reset_participant_start(101)
        self.engine.forget_tag(EPC.lower())
        self.assertIsNone(self.db.get_participant_by_bib(101)["start_time"])
        self.feed(reset_at, MAX_BURST_SECONDS + 2.0)            # still parked, never left
        start = self.db.get_participant_by_bib(101)["start_time"]
        self.assertIsNotNone(start, "a reset runner whose tag stays on the mat starts again")
        self.assertGreaterEqual(start, reset_at, "reads from before the reset don't count")
        self.assertEqual(self.durations(), [])

    def test_forget_tag_ignores_runners_without_a_tag(self):
        self.engine.forget_tag(None)
        self.engine.forget_tag("")

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

    def test_intermittent_fringe_reads_never_trigger_phantom_laps(self):
        """A runner chatting 2-3 m from the mat gets read every ~6 s (wider than crossing_window,
        so self.lingering never engages). Even after 300 s (> 6x min_lap_seconds), zero phantom
        laps are recorded until they actually leave the field for >= min_lap_seconds."""
        self.db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=600.0)
        self.db.start_event(at=1000.0)
        too_soon = self.too_soon_events()
        self.cross(1000.0)   # Start
        self.cross(1100.0)   # Real Lap 1 (100s)
        self.assertEqual(self.durations(), [100.0])

        # Intermittent fringe reads every 6 s for 180 s (each burst flushes with quiet=True)
        for step in range(1, 31):
            t = 1100.0 + step * 6.0
            self.engine.process_tag_read(TagRead(epc=EPC, timestamp=t, rssi=-74.0))
            asyncio.run(self.engine.flush_once(t + 2.0))

        self.assertEqual(self.durations(), [100.0], "intermittent reads near mat must not record phantom laps")
        self.assertEqual(len(too_soon), 1, "too-soon prompt fires once, not on every 6 s fringe ping")

        # Now the runner actually leaves for 70 s (>= 45 s min_lap) and finishes Lap 2
        self.engine.process_tag_read(TagRead(epc=EPC, timestamp=1280.0 + 70.0, rssi=-44.0))
        self.leave(1350.0)
        self.assertEqual(self.durations(), [100.0, 250.0])

    def test_lap_requires_min_lap_silence_since_last_read(self):
        """Even if >= min_lap_seconds has passed since the previous crossing, a read < min_lap_seconds
        after a debounced stray read is still debounced."""
        self.db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=600.0)
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        self.assertEqual(self.cross(1030.0), "debounced")
        # 55 s since start (>= 45 s), but only 25 s since the 1030.0 read (< 45 s)
        self.assertEqual(self.cross(1055.0), "debounced")
        self.assertEqual(self.durations(), [])
        # 50 s of true silence after 1055.0 -> valid lap!
        self.assertEqual(self.cross(1105.0), "lap")
        self.assertEqual(self.durations(), [105.0])


class ManualBreakTest(EngineTestCase):
    """Issue #86: the mat and the Break/Resume buttons must agree about a runner's rest."""

    def breaks(self):
        return [(b["start_time"], b["end_time"], b["duration_seconds"])
                for b in self.db.get_breaks_for_runner(101)]

    def test_crossing_during_a_manual_break_ends_it_instead_of_making_a_lap(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        resumed = []
        self.engine.on_runner_resumed = resumed.append
        with self.at(1010.0):
            self.db.start_manual_break(101)
        self.assertEqual(self.cross(1300.0), "resumed")
        self.assertEqual(self.durations(), [])
        self.assertEqual(self.breaks(), [(1010.0, 1300.0, 290.0)])
        self.assertIsNone(self.db.get_open_break(101))
        self.assertEqual(self.db.get_participant_by_bib(101)["last_crossing_time"], 1300.0)
        self.assertEqual([r["break_duration_seconds"] for r in resumed], [290.0])
        # The next crossing is an ordinary lap from the resume, and End adds no rest
        self.assertEqual(self.cross(1360.0), "lap")
        self.assertEqual(self.durations(), [60.0])
        self.db.end_event(at=8400.0)
        self.assertEqual(self.breaks(), [(1010.0, 1300.0, 290.0)])

    def test_long_manual_break_is_not_also_an_auto_break(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        with self.at(1010.0):
            self.db.start_manual_break(101)
        self.assertEqual(self.cross(2000.0), "resumed")   # past the 600 s auto-break threshold
        self.assertEqual(self.breaks(), [(1010.0, 2000.0, 990.0)])

    def test_resume_on_an_auto_flagged_rest_keeps_the_rest_time(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        with self.at(1900.0):                              # 900 s since the last crossing
            result = self.db.end_manual_break(101)
        self.assertEqual(result["duration_seconds"], 900.0)
        self.assertEqual(self.breaks(), [(1000.0, 1900.0, 900.0)])
        self.assertEqual(self.db.get_participant_by_bib(101)["last_crossing_time"], 1900.0)

    def test_resume_on_a_runner_who_is_not_resting_changes_nothing(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        with self.at(1030.0):                              # a stale or double-clicked Resume
            self.assertEqual(self.db.end_manual_break(101)["status"], "not_on_break")
        self.assertEqual(self.breaks(), [])
        self.assertEqual(self.cross(1060.0), "lap")
        self.assertEqual(self.durations(), [60.0])

    def test_accepted_too_soon_crossing_ends_a_manual_break(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        with self.at(1005.0):
            self.db.start_manual_break(101)
        self.assertEqual(self.cross(1015.0), "debounced")  # back after 10 s: below the min lap
        self.assertIsNotNone(self.db.get_open_break(101))
        result = asyncio.run(self.engine.accept_too_soon_crossing(101, 1015.0))
        self.assertTrue(result["resumed"])
        self.assertEqual(self.breaks(), [(1005.0, 1015.0, 10.0)])
        self.assertEqual(self.cross(1075.0), "lap")
        self.assertEqual(self.durations(), [60.0])

    def test_crossing_read_as_end_is_pressed_ends_the_break_there(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        with self.at(1010.0):
            self.db.start_manual_break(101)
        self.db.end_event(at=1301.0)                       # the 1300 crossing is still buffered
        self.assertEqual(self.cross(1300.0), "resumed")
        self.assertEqual(self.durations(), [])
        self.assertEqual(self.breaks(), [(1010.0, 1300.0, 290.0)])

    def test_resume_closes_an_open_manual_break(self):
        self.db.start_event(at=1000.0)
        self.cross(1000.0)
        with self.at(1010.0):
            self.db.start_manual_break(101)
        with self.at(1100.0):
            self.assertEqual(self.db.end_manual_break(101)["duration_seconds"], 90.0)
        self.assertEqual(self.breaks(), [(1010.0, 1100.0, 90.0)])


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
