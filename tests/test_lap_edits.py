"""Manual laps, deleting laps and splitting laps keep lap times consistent (issue #9)."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from core.db import Database, LapEditError
from core.lap_engine import LapEngine

EPC = "E28011700000020101010001"


class LapEditTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(os.path.join(self.tmp.name, "race.db"))
        self.db.add_participant(101, "Alex", EPC)
        self.db.update_event_settings(min_lap_seconds=20.0, max_lap_rest_seconds=300.0)
        self.db.start_event(at=1000.0)
        self.engine = LapEngine(db=self.db, crossing_window=1.0)
        self.cross(1000.0)  # chip start

    def cross(self, t):
        return asyncio.run(self.engine._resolve_crossing(EPC, [(t, -50.0)]))

    def at(self, t):
        return mock.patch("core.clock.now", return_value=t)

    def laps(self):
        return [(lap["lap_number"], lap["lap_duration_seconds"]) for lap in self.db.get_laps_for_runner(101)]

    def last_crossing(self):
        return self.db.get_participant_by_bib(101)["last_crossing_time"]

    def lap_id(self, number):
        return next(lap["id"] for lap in self.db.get_laps_for_runner(101) if lap["lap_number"] == number)


class ManualLapTest(LapEditTestCase):
    def test_manual_lap_then_auto_lap_is_not_double_counted(self):
        self.cross(1060.0)                               # lap 1: 60 s
        with self.at(1200.0):
            self.db.add_manual_lap(101, duration=70.0)   # reader missed lap 2, ended at 1130
        self.cross(1195.0)                               # lap 3, timed from the manual crossing
        self.assertEqual(self.laps(), [(1, 60.0), (2, 70.0), (3, 65.0)])

    def test_manual_lap_without_duration_ends_now(self):
        with self.at(1075.0):
            lap = self.db.add_manual_lap(101)
        self.assertEqual((lap["lap_number"], lap["lap_duration_seconds"], lap["crossing_time"]), (1, 75.0, 1075.0))
        self.assertEqual(lap["name"], "Alex")
        self.assertEqual(self.last_crossing(), 1075.0)

    def test_rejected_manual_laps_change_nothing(self):
        cases = [
            (dict(duration=500.0), 1100.0, "would end in the future"),
            (dict(duration=-5.0), 1100.0, "positive"),
            (dict(min_gap=20.0), 1010.0, "Only 10s"),
        ]
        for kwargs, now, message in cases:
            with self.subTest(kwargs=kwargs), self.at(now), self.assertRaisesRegex(LapEditError, message):
                self.db.add_manual_lap(101, **kwargs)
        self.assertEqual(self.laps(), [])
        self.assertEqual(self.last_crossing(), 1000.0)

    def test_runner_must_have_started(self):
        self.db.add_participant(102, "Sam", "E28011700000020101010002")
        with self.assertRaisesRegex(LapEditError, "hasn't started"):
            self.db.add_manual_lap(102, duration=60.0)
        with self.assertRaisesRegex(LapEditError, "isn't registered"):
            self.db.add_manual_lap(999, duration=60.0)


class DeleteLapTest(LapEditTestCase):
    def test_deleting_a_middle_lap_merges_it_into_the_next(self):
        for t in (1060.0, 1075.0, 1140.0):                # lap 2 is a ghost read 15 s after lap 1
            self.db.update_event_settings(min_lap_seconds=10.0)
            self.cross(t)
        self.assertEqual(self.laps(), [(1, 60.0), (2, 15.0), (3, 65.0)])
        removed = self.db.delete_lap(self.lap_id(2))
        self.assertEqual(removed["lap_duration_seconds"], 15.0)
        self.assertEqual(self.laps(), [(1, 60.0), (2, 80.0)])  # renumbered, total time kept

    def test_deleting_the_last_lap_times_the_next_crossing_from_before_it(self):
        self.db.update_event_settings(min_lap_seconds=10.0)
        self.cross(1060.0)
        self.cross(1075.0)                                # ghost lap 2
        self.db.delete_lap(self.lap_id(2))
        self.assertEqual(self.last_crossing(), 1060.0)
        self.cross(1125.0)                                # the real lap 2
        self.assertEqual(self.laps(), [(1, 60.0), (2, 65.0)])

    def test_deleting_the_last_lap_keeps_a_later_pause_skipped(self):
        self.cross(1060.0)
        self.cross(1120.0)                                # lap 2, then a 100 s pause
        with self.at(1130.0):
            self.db.pause_event()
        self.db.start_event(at=1230.0)
        self.db.delete_lap(self.lap_id(2))
        self.cross(1300.0)                                # 240 s since lap 1, 100 of them paused
        self.assertEqual(self.laps(), [(1, 60.0), (2, 140.0)])

    def test_deleting_a_lap_before_a_break_extends_the_break(self):
        self.cross(1060.0)                                # lap 1
        self.cross(1080.0)                                # ghost lap 2 at 1080
        self.cross(1500.0)                                # 420 s > 300: break 1080 -> 1500
        self.cross(1570.0)                                # lap 3
        self.db.delete_lap(self.lap_id(2))
        self.assertEqual(self.laps(), [(1, 60.0), (2, 70.0)])
        brk = self.db.get_breaks_for_runner(101)[0]
        self.assertEqual((brk["start_time"], brk["duration_seconds"]), (1060.0, 440.0))

    def test_delete_last_lap_for_runner(self):
        self.cross(1060.0)
        self.cross(1130.0)
        self.assertEqual(self.db.delete_last_lap_for_runner(101)["lap_number"], 2)
        self.assertEqual(self.last_crossing(), 1060.0)
        self.db.delete_last_lap_for_runner(101)
        self.assertEqual(self.last_crossing(), 1000.0)    # back to the chip start
        self.assertIsNone(self.db.delete_last_lap_for_runner(101))
        self.assertIsNone(self.db.delete_lap(987654))


class SplitLapTest(LapEditTestCase):
    def test_split_keeps_lap_numbers_unique(self):
        for t in (1060.0, 1180.0, 1240.0, 1300.0):        # lap 2 is a double (120 s)
            self.cross(t)
        self.db.split_lap(self.lap_id(2))
        # Previously the old lap 3 kept number 3 next to the split's new 3 (two lap 3s / 4s)
        self.assertEqual(self.laps(), [(1, 60.0), (2, 60.0), (3, 60.0), (4, 60.0), (5, 60.0)])

    def test_split_stays_in_the_laps_own_event(self):  # issue #93
        for t in (1060.0, 1180.0):                        # lap 2 is a double (120 s)
            self.cross(t)
        first = self.db.current_event_id
        double = self.lap_id(2)
        self.db.end_event(at=1200.0)
        second = self.db.create_event("Second")
        self.db.set_current_event(second)
        self.db.add_participant(101, "Someone else", "E28011700000020101010009")
        self.db.split_lap(double)                         # e.g. from a stale admin tab
        self.assertEqual([(lap["lap_number"], lap["lap_duration_seconds"])
                          for lap in self.db.get_laps_for_runner(101, event_id=first)],
                         [(1, 60.0), (2, 60.0), (3, 60.0)])
        self.assertEqual(self.db.get_laps_for_runner(101, event_id=second), [])


class ManualLapApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:
            raise unittest.SkipTest(str(e))
        os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "lap_edits_import.db"))
        os.environ.setdefault("MOCK_HARDWARE", "true")
        from web import app as web_app
        cls.tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(cls.tmp.name, "lap_edits_api.db"))  # web.app is shared across modules
        db.add_participant(101, "Alex", EPC)
        cls.patches = [mock.patch.object(web_app, "db", db), mock.patch.object(web_app.lap_engine, "db", db)]
        for p in cls.patches:
            p.start()
        cls.db = db
        cls.client = TestClient(web_app.app)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def test_manual_lap_endpoint(self):
        with mock.patch("core.clock.now", return_value=5000.0):
            self.db.start_event()
        self.db.set_participant_start(101, 5000.0)
        with mock.patch("core.clock.now", return_value=5090.0):
            too_long = self.client.post("/api/laps/manual", json={"bib_number": 101, "lap_duration_seconds": 300})
            self.assertEqual(too_long.status_code, 409)
            self.assertIn("future", too_long.json()["detail"])
            ok = self.client.post("/api/laps/manual", json={"bib_number": 101})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["lap"]["lap_duration_seconds"], 90.0)
        self.assertEqual(self.client.post("/api/laps/manual", json={"bib_number": 999}).status_code, 404)
        self.assertEqual(self.client.delete("/api/laps/987654").status_code, 404)
        self.db.end_event()
        self.assertEqual(self.client.post("/api/laps/manual", json={"bib_number": 101}).status_code, 409)


class EndedEventApiTest(unittest.TestCase):
    """End freezes the results: lap and runner edits wait for Reopen (issue #93)."""

    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:
            raise unittest.SkipTest(str(e))
        os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "lap_edits_import.db"))
        os.environ.setdefault("MOCK_HARDWARE", "true")
        from web import app as web_app
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = db = Database(os.path.join(tmp.name, "ended_api.db"))
        for p in (mock.patch.object(web_app, "db", db), mock.patch.object(web_app.lap_engine, "db", db)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(web_app.app)
        db.add_participant(101, "Alex", EPC)
        db.add_participant(102, "Sam", "E28011700000020101010002")
        db.add_participant(103, "Kim", "E28011700000020101010003")
        db.start_event(at=1000.0)
        db.set_participant_start(101, 1000.0)
        db.set_participant_start(103, 1000.0)
        db.record_lap(101, 1, 1060.0, 60.0)
        self.double = db.record_lap(101, 2, 1180.0, 120.0, flag_reason="Possible missed lap")
        db.update_participant_last_crossing(101, 1180.0)
        with mock.patch("core.clock.now", return_value=1190.0):
            db.start_manual_break(103)
        db.end_event(at=1200.0)
        self.event_id = db.current_event_id

    def snapshot(self):
        return ([(lap["id"], lap["lap_duration_seconds"], lap["flag_reason"]) for lap in self.db.get_all_laps(self.event_id)],
                [(p["bib_number"], p["start_time"]) for p in self.db.get_all_participants(self.event_id)],
                [(b["bib_number"], b["end_time"]) for bib in (101, 102, 103)
                 for b in self.db.get_breaks_for_runner(bib, event_id=self.event_id)])

    def assert_frozen(self):
        before = self.snapshot()
        for method, path in [("DELETE", f"/api/laps/{self.double}"), ("POST", f"/api/laps/{self.double}/split"),
                             ("POST", f"/api/laps/{self.double}/accept"), ("DELETE", "/api/participants/101"),
                             ("POST", "/api/participants/102/start"), ("POST", "/api/participants/101/reset"),
                             ("POST", "/api/participants/101/break/start"), ("POST", "/api/participants/103/break/end")]:
            with self.subTest(path=path):
                res = self.client.request(method, path)
                self.assertEqual(res.status_code, 409, res.text)
                self.assertIn("ended", res.json()["detail"])
        self.assertEqual(self.snapshot(), before)

    def test_ended_event_refuses_edits(self):
        self.assert_frozen()
        self.assertEqual(self.client.get(f"/api/events/{self.event_id}/results.csv").status_code, 200)
        self.assertEqual(self.client.delete("/api/laps/987654").status_code, 404)

    def test_checked_against_the_laps_own_event(self):
        """A stale tab can still point at an ended event's laps after a switch to a new event."""
        self.db.set_current_event(self.db.create_event("Second"))
        self.assertEqual(self.client.delete(f"/api/laps/{self.double}").status_code, 409)
        self.assertEqual(self.client.post(f"/api/laps/{self.double}/split").status_code, 409)
        self.assertEqual(self.client.post(f"/api/laps/{self.double}/accept").status_code, 409)
        self.assertEqual(len(self.db.get_all_laps(self.event_id)), 2)

    def test_reopen_allows_edits_again(self):
        self.client.post("/api/event/reopen")
        self.assertEqual(self.client.post(f"/api/laps/{self.double}/split").status_code, 200)
        self.assertEqual([lap["lap_duration_seconds"] for lap in self.db.get_laps_for_runner(101)], [60.0, 60.0, 60.0])
        self.assertEqual(self.client.delete("/api/participants/102").status_code, 200)


if __name__ == "__main__":
    unittest.main()
