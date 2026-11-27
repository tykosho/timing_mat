"""A crossing whose resolution fails part-way (a transient DB error, a broken callback) is
retried on the next tick, and counted exactly once."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from core.db import Database
from core.lap_engine import LapEngine
from core.models import TagRead

EPC = "E28011700000020101010001"
EPC_B = "E28011700000020101010002"


def failing_once(real, error=None):
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise error or sqlite3.OperationalError("disk I/O error")
        return real(*args, **kwargs)
    return wrapper


class CrossingRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(os.path.join(self.tmp.name, "race.db"))
        self.db.add_participant(101, "Alex", EPC)
        self.db.add_participant(102, "Sam", EPC_B)
        self.db.update_event_settings(min_lap_seconds=20.0, max_lap_rest_seconds=600.0)
        self.engine = LapEngine(db=self.db, crossing_window=1.0)
        self.db.start_event(at=1000.0)
        self.engine.refresh_event()                  # as flush_loop does every tick
        asyncio.run(self.engine._resolve_crossing(EPC, [(1000.0, -50.0)]))
        asyncio.run(self.engine._resolve_crossing(EPC_B, [(1000.0, -50.0)]))

    def read(self, t, epc=EPC):
        self.engine.process_tag_read(TagRead(epc=epc, timestamp=t, rssi=-50.0))

    def flush(self, t):
        asyncio.run(self.engine.flush_once(t))

    def laps(self, bib=101):
        return [lap["lap_duration_seconds"] for lap in self.db.get_laps_for_runner(bib)]

    def test_db_error_while_recording_a_lap_retries_next_tick(self):
        self.read(1060.0)
        with mock.patch.object(self.db, "record_lap", side_effect=failing_once(self.db.record_lap)):
            with self.assertRaises(sqlite3.OperationalError):
                self.flush(1062.0)                  # the tick fails: flush_loop logs and retries
            self.assertEqual(self.laps(), [])
            self.flush(1063.0)
        self.assertEqual(self.laps(), [60.0])
        self.flush(1064.0)
        self.assertEqual(self.laps(), [60.0], "exactly once")

    def test_failure_after_the_lap_was_written_does_not_count_it_twice(self):
        self.read(1060.0)
        self.engine.on_lap_completed = failing_once(lambda lap: None, RuntimeError("broadcast failed"))
        with self.assertRaises(RuntimeError):
            self.flush(1062.0)
        self.flush(1063.0)
        self.assertEqual(self.laps(), [60.0])

    def test_other_runners_are_not_held_up_by_a_failing_one(self):
        self.read(1060.0)
        self.read(1060.5, epc=EPC_B)
        real = self.db.record_lap

        def fail_for_alex(*args, **kwargs):
            if kwargs.get("bib_number") == 101:
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)
        with mock.patch.object(self.db, "record_lap", side_effect=fail_for_alex):
            with self.assertRaises(sqlite3.OperationalError):
                self.flush(1062.0)
        self.assertEqual(self.laps(102), [60.5])
        self.flush(1063.0)
        self.assertEqual(self.laps(101), [60.0])

    def test_a_later_crossing_waits_for_the_failed_one(self):
        self.read(1060.0)
        with mock.patch.object(self.db, "record_lap", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                self.flush(1062.0)
            self.read(1120.0)                        # the next lap, while the disk is still failing
            with self.assertRaises(sqlite3.OperationalError):
                self.flush(1122.0)
        self.flush(1123.0)
        self.assertEqual(self.laps(), [60.0, 60.0], "both laps, in order, neither merged")


    def test_next_visit_queued_behind_a_retried_parked_burst_still_counts(self):
        for i in range(17):                          # parked 1050-1058: force-resolved burst
            self.read(1050.0 + i * 0.5)
        with mock.patch.object(self.db, "record_lap", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                self.flush(1058.0)
        self.read(1118.0)                            # left, ran a lap, back while it retries
        self.flush(1120.0)
        self.assertEqual(self.laps(), [50.0, 68.0])

    def test_failed_crossing_from_before_end_survives_end(self):
        self.read(1060.0)
        with mock.patch.object(self.db, "record_lap", side_effect=failing_once(self.db.record_lap)):
            with self.assertRaises(sqlite3.OperationalError):
                self.flush(1062.0)
            self.db.end_event(at=1062.5)
            self.engine.refresh_event()
            self.flush(1063.0)
        self.assertEqual(self.laps(), [60.0])

    def test_retry_overtaken_by_a_manual_lap_is_dropped_quietly(self):
        self.read(1060.0)
        with mock.patch.object(self.db, "record_lap", side_effect=failing_once(self.db.record_lap)):
            with self.assertRaises(sqlite3.OperationalError):
                self.flush(1062.0)
            self.db.update_participant_last_crossing(101, 1065.0)   # e.g. an operator's +1 lap
            self.flush(1066.0)
        self.assertEqual(self.engine.failed_bursts, [])

if __name__ == "__main__":
    unittest.main()
