"""Each multi-step state change is one transaction (issue #88): a failure part-way through
leaves nothing half-written. The failure is injected with a trigger on the second write."""
import asyncio
import os
import sqlite3
import tempfile
import unittest

from core.db import Database
from core.lap_engine import LapEngine

EPC = "E28011700000020101010001"


class SingleTransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "race.db")
        self.db = Database(self.path)
        self.db.add_participant(101, "Alex", EPC)
        self.db.update_event_settings(min_lap_seconds=20.0, max_lap_rest_seconds=600.0)
        self.engine = LapEngine(db=self.db, crossing_window=1.0)
        self.db.start_event(at=1000.0)
        self.cross(1000.0)

    def cross(self, t):
        return asyncio.run(self.engine._resolve_crossing(EPC, [(t, -50.0)]))

    def fail_on_update(self, table):
        with sqlite3.connect(self.path) as conn:
            conn.execute(f"""CREATE TRIGGER fail_{table} BEFORE UPDATE ON {table}
                             BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""")

    def last_crossing(self):
        return self.db.get_participant_by_bib(101)["last_crossing_time"]

    def test_lap_and_last_crossing_are_written_together(self):
        self.fail_on_update("participants")
        with self.assertRaises(sqlite3.IntegrityError):
            self.cross(1060.0)
        self.assertEqual(self.db.get_laps_for_runner(101), [], "no lap without its last crossing")
        self.assertEqual(self.last_crossing(), 1000.0)

    def test_auto_break_and_last_crossing_are_written_together(self):
        self.fail_on_update("participants")
        with self.assertRaises(sqlite3.IntegrityError):
            self.cross(1900.0)                      # past the 600 s rest threshold
        self.assertEqual(self.db.get_breaks_for_runner(101), [])

    def test_resume_shift_and_status_are_written_together(self):
        self.db._set_status(self.db.get_event(), "paused", 1000.0, None, paused_at=1010.0)
        self.fail_on_update("events")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.start_event(at=1100.0)
        self.assertEqual(self.db.get_event()["status"], "paused")
        self.assertEqual(self.last_crossing(), 1000.0, "pressing Resume again must not shift twice")

    def test_end_closes_breaks_and_sets_status_together(self):
        self.db.start_manual_break(101)
        self.fail_on_update("events")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.end_event(at=1100.0)
        self.assertEqual(self.db.get_event()["status"], "running")
        self.assertIsNotNone(self.db.get_open_break(101))

    def test_missed_start_flag_is_written_with_the_start(self):
        self.db.add_participant(102, "Sam", "E28011700000020101010002")
        self.fail_on_update("participants")
        with self.assertRaises(sqlite3.IntegrityError):
            asyncio.run(self.engine._resolve_crossing("E28011700000020101010002", [(1300.0, -50.0)]))
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TRIGGER fail_participants;")
        asyncio.run(self.engine._resolve_crossing("E28011700000020101010002", [(1300.0, -50.0)]))
        sam = self.db.get_participant_by_bib(102)
        self.assertEqual(sam["start_time"], 1300.0)
        self.assertIsNotNone(sam["start_flag_reason"], "300 s after the gun: flagged for review")

    def test_normal_lap_still_moves_the_last_crossing(self):
        self.assertEqual(self.cross(1060.0), "lap")
        self.assertEqual(self.last_crossing(), 1060.0)
        self.assertEqual(self.cross(1900.0), "resumed")
        self.assertEqual(self.last_crossing(), 1900.0)


if __name__ == "__main__":
    unittest.main()
