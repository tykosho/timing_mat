"""Race time across app restarts, reboots and clock corrections (issue #128)."""
import asyncio
import math
import os
import tempfile
import unittest
from unittest import mock

from core import clock as clock_module
from core.clock import MAX_DOWNTIME_SECONDS, RaceClock
from core.db import RESTART_FLAG_REASON, Database
from core.lap_engine import CLOCK_SAVE_INTERVAL, END_SETTLE_SECONDS, LapEngine
from core.models import TagRead

EPC = "E28011700000020101010001"
NOW = 1_795_000_000.0       # the real time
WEEK = 582_295.0            # how stale the Pi's clock was on 2026-11-22


class FakePi:
    """Wall clock, uptime and boot id of a Pi without an RTC."""
    def __init__(self, wall):
        self.wall_t, self.up, self.boot = wall, 40.0, "boot-1"

    def advance(self, seconds):
        self.wall_t += seconds
        self.up += seconds

    def reboot(self, wall, up=35.0):
        self.wall_t, self.up, self.boot = wall, up, self.boot + "+"


class RestartTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "race.db")
        self.pi = FakePi(NOW - WEEK)
        patcher = mock.patch.object(clock_module, "read_boot_id", lambda: self.pi.boot)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock_patch = None
        self.addCleanup(lambda: self.clock_patch and self.clock_patch.stop())
        self.launch()
        self.db.add_participant(1, "Alex", EPC)
        self.db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=0.0)

    def launch(self):
        """The app starting: new clock and engine, same database."""
        if self.clock_patch:
            self.clock_patch.stop()
        self.clock_patch = mock.patch.object(
            clock_module, "clock", RaceClock(wall=lambda: self.pi.wall_t, mono=lambda: self.pi.up))
        self.clock_patch.start()
        self.db = Database(self.path)
        self.engine = LapEngine(db=self.db, crossing_window=0.5)
        self.engine.resume_clock()

    def start(self):
        self.engine.prepare_start()
        self.db.start_event()
        self.engine.save_clock(force=True)

    def cross(self):
        return asyncio.run(self.engine._resolve_crossing(EPC, [(clock_module.now(), -50.0)]))

    def run_on(self, seconds):
        self.pi.advance(seconds)
        self.engine.save_clock()

    def ntp(self, seconds=WEEK):
        self.pi.wall_t += seconds
        self.engine.save_clock()

    def last_lap(self):
        lap = self.db.get_laps_for_runner(1)[-1]
        return lap["lap_duration_seconds"], lap["flag_reason"]

    def race_with_corrected_clock(self):
        """Started a week in the past, NTP arrived, 30 s into a lap."""
        self.start()
        self.cross()
        self.run_on(300)
        self.ntp()
        self.run_on(100)
        self.cross()
        self.run_on(30)


class SameBootTest(RestartTestCase):
    def test_restart_after_ntp_is_exact_and_not_flagged(self):
        self.race_with_corrected_clock()
        self.pi.advance(8)
        self.launch()
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (78.0, None))

    def test_exact_whatever_the_wall_clock_did_while_down(self):
        for jump in (-7200.0, 16.0, 5 * WEEK):
            with self.subTest(jump=jump):
                self.setUp()
                self.race_with_corrected_clock()
                self.pi.advance(8)
                self.pi.wall_t += jump
                self.launch()
                self.run_on(40)
                self.cross()
                self.assertEqual(self.last_lap(), (78.0, None))

    def test_exact_from_an_older_periodic_save(self):
        self.race_with_corrected_clock()
        self.pi.advance(CLOCK_SAVE_INTERVAL - 1)  # crashed without saving again
        self.launch()
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (30 + CLOCK_SAVE_INTERVAL - 1 + 40, None))


class RebootTest(RestartTestCase):
    def test_the_nov_22_incident(self):
        self.race_with_corrected_clock()
        self.pi.reboot(self.pi.wall_t + 60)
        self.launch()
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (130.0, RESTART_FLAG_REASON))

    def test_stale_boot_clock_counts_at_least_the_uptime(self):
        self.race_with_corrected_clock()
        self.pi.reboot(self.pi.wall_t - 8)
        self.launch()
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (30 + 35 + 40, RESTART_FLAG_REASON))

    def test_correction_while_off_is_not_time_off(self):
        self.start()
        self.cross()
        self.run_on(30)                    # never synced
        self.pi.reboot(NOW + 90)           # right clock before the app starts: a week "later"
        self.launch()
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (30 + 35 + 40, RESTART_FLAG_REASON))

    def test_downtime_limit(self):
        self.start()
        saved = self.db.get_clock_state()
        for moved, counted in ((MAX_DOWNTIME_SECONDS, MAX_DOWNTIME_SECONDS),
                               (MAX_DOWNTIME_SECONDS + 1, 35.0), (-1.0, 35.0)):
            with self.subTest(moved=moved):
                clock = RaceClock(wall=lambda: saved["wall"] + moved, mono=lambda: 35.0)
                with mock.patch.object(clock_module, "read_boot_id", lambda: "other"):
                    self.assertFalse(clock.resume(saved, None))
                self.assertEqual(clock.now(), saved["race"] + counted)

    def test_only_the_lap_that_spans_it_is_flagged(self):
        self.race_with_corrected_clock()
        self.pi.reboot(self.pi.wall_t + 60)
        self.launch()
        self.run_on(40)
        self.cross()
        self.run_on(90)
        self.cross()
        self.assertEqual(self.last_lap(), (90.0, None))
        self.assertEqual(len(self.db.get_flagged_laps()), 1)

    def test_flag_survives_another_restart(self):
        self.race_with_corrected_clock()
        self.pi.reboot(self.pi.wall_t + 60)
        self.launch()
        self.run_on(10)
        self.launch()                      # exact, but the lap still spans the reboot
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (140.0, RESTART_FLAG_REASON))

    def test_reboot_during_a_pause_costs_nothing(self):
        self.race_with_corrected_clock()
        self.db.pause_event()
        self.pi.reboot(self.pi.wall_t + 60)
        self.launch()
        self.run_on(20)
        self.db.start_event()
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (70.0, None))

    def test_reboot_before_a_long_pause_is_still_flagged(self):
        self.race_with_corrected_clock()
        self.pi.reboot(self.pi.wall_t + 60)
        self.launch()
        self.run_on(5)
        self.db.pause_event()
        self.run_on(600)                   # shifts last_crossing_time to well past the reboot
        self.db.start_event()
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (135.0, RESTART_FLAG_REASON))

    def test_confirmed_too_soon_crossing_is_flagged(self):
        self.race_with_corrected_clock()
        self.pi.reboot(self.pi.wall_t)
        self.launch()
        asyncio.run(self.engine.accept_too_soon_crossing(1, clock_module.now() + 1))
        self.assertEqual(self.last_lap()[1], RESTART_FLAG_REASON)

    def test_manual_lap_timed_now_is_flagged_but_typed_duration_is_not(self):
        self.race_with_corrected_clock()
        self.pi.reboot(self.pi.wall_t + 60)
        self.launch()
        self.assertEqual(self.db.add_manual_lap(1)["flag_reason"], RESTART_FLAG_REASON)
        self.run_on(100)
        self.assertIsNone(self.db.add_manual_lap(1, duration=50.0)["flag_reason"])


class DamagedOrMissingStateTest(RestartTestCase):
    def forget_clock(self, value=None):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = 'race_clock';")
            if value is not None:
                conn.execute("INSERT INTO settings VALUES ('race_clock', ?);", (value,))

    def test_upgrade_mid_race_does_not_add_the_correction(self):
        self.race_with_corrected_clock()
        self.forget_clock()                # stored by a version without #128
        self.pi.advance(3 * 3600)          # uptime must not count as downtime either
        self.launch()
        self.run_on(60)
        self.cross()
        duration, reason = self.last_lap()
        self.assertLess(duration, 120)
        self.assertEqual(reason, RESTART_FLAG_REASON)

    def test_damaged_state_never_breaks_startup(self):
        self.race_with_corrected_clock()
        for value in ("", "not json", "[]", "{}", '{"race": "x"}', '{"race": NaN, "wall": 1, "mono": 1}'):
            with self.subTest(value=value):
                self.forget_clock(value)
                self.launch()
                self.assertTrue(math.isfinite(clock_module.now()))
                self.assertLess(clock_module.now(), NOW - WEEK + 3600)

    def test_never_behind_stored_times(self):
        self.start()
        ahead = clock_module.now() + 500
        self.db.insert_raw_reads([(self.db.get_event()["id"], EPC, ahead, -50.0)])
        self.pi.reboot(self.pi.wall_t)
        self.launch()
        self.assertGreater(clock_module.now(), ahead)

    def test_no_boot_id_is_estimated_not_trusted(self):
        self.pi.boot = None                # e.g. macOS
        self.race_with_corrected_clock()
        self.pi.advance(8)
        self.launch()
        self.run_on(40)
        self.cross()
        self.assertEqual(self.last_lap(), (78.0, RESTART_FLAG_REASON))


class SavingTest(RestartTestCase):
    def test_jump_is_saved_at_once_and_synced(self):
        self.start()
        with mock.patch.object(self.db, "save_clock_state") as save:
            self.run_on(1)
            save.assert_not_called()
            self.ntp()
            self.assertTrue(save.call_args.kwargs["durable"])

    def test_periodic_save_is_not_synced(self):
        self.start()
        with mock.patch.object(self.db, "save_clock_state") as save:
            self.run_on(CLOCK_SAVE_INTERVAL)
            self.assertFalse(save.call_args.kwargs["durable"])

    def test_failed_save_does_not_stop_timing(self):
        self.start()
        with mock.patch.object(self.db, "save_clock_state", side_effect=OSError("disk full")):
            with self.assertLogs("LapEngine", level="ERROR"):
                self.ntp()
            self.assertEqual(self.cross(), "started")

    def test_nothing_is_written_between_races(self):
        self.run_on(CLOCK_SAVE_INTERVAL * 2)
        self.assertIsNone(self.db.get_clock_state())


class StartTest(RestartTestCase):
    def test_start_takes_the_time_just_set(self):
        self.pi.wall_t += WEEK
        self.start()
        self.assertAlmostEqual(self.db.get_event()["start_time"], NOW, delta=1)

    def test_resume_from_pause_keeps_the_race_timeline(self):
        self.start()
        before = clock_module.now()
        self.db.pause_event()
        self.ntp()
        self.start()
        self.assertEqual(clock_module.now(), before)

    def test_start_on_an_ended_event_moves_nothing(self):
        self.start()
        self.db.end_event()
        before = clock_module.now()
        self.ntp()
        self.engine.prepare_start()
        self.assertEqual(clock_module.now(), before)

    def test_tag_waiting_at_the_line_cannot_start_a_week_early(self):
        old = clock_module.now()
        self.engine.process_tag_read(TagRead(epc=EPC, timestamp=old, rssi=-50.0))
        self.pi.wall_t += WEEK
        self.start()
        self.engine.process_tag_read(TagRead(epc=EPC, timestamp=old + 0.1, rssi=-50.0))  # still queued
        self.assertEqual(self.engine.buffers, {})
        self.engine.process_tag_read(TagRead(epc=EPC, timestamp=clock_module.now(), rssi=-50.0))
        self.run_on(2)
        asyncio.run(self.engine.flush_once(clock_module.now()))
        self.assertGreaterEqual(self.db.get_participant_by_bib(1)["start_time"], self.db.get_event()["start_time"])

    def test_crossing_just_before_end_survives_a_pending_resync(self):
        self.start()
        self.cross()
        self.run_on(80)
        self.ntp()
        self.engine.process_tag_read(TagRead(epc=EPC, timestamp=clock_module.now(), rssi=-50.0))
        self.pi.advance(0.2)
        self.db.end_event()
        self.engine.maintain_clock()
        self.pi.advance(2)
        asyncio.run(self.engine.flush_once(clock_module.now()))
        self.assertEqual(self.last_lap(), (80.0, None))
        self.pi.advance(END_SETTLE_SECONDS)
        self.engine.maintain_clock()
        self.assertAlmostEqual(clock_module.now(), self.pi.wall_t, delta=1)


if __name__ == "__main__":
    unittest.main()
