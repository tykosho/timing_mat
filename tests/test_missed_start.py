"""A runner whose tag isn't read in the crowd at the gun (issue #72)."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from core.db import Database, LapEditError
from core.lap_engine import LapEngine

GUN = 1000.0
EPC = "E2000000000000000000AA01"


class MissedStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(os.path.join(self.tmp.name, "race.db"))
        self.db.update_event_settings(min_lap_seconds=45.0)
        self.db.add_participant(7, "Pat", EPC)
        self.db.add_participant(8, "Sam", "E2000000000000000000AA02")
        self.db.start_event(at=GUN)
        self.started = []
        self.engine = LapEngine(self.db, on_runner_started=self.started.append)

    def cross(self, t, epc=EPC, force=False):
        return asyncio.run(self.engine._resolve_crossing(epc, [(t, -45.0)], force=force))

    def flag(self, bib=7):
        return self.db.get_participant_by_bib(bib)["start_flag_reason"]

    def test_start_at_the_gun_is_not_flagged(self):
        self.assertEqual(self.cross(GUN + 3), "started")
        self.assertIsNone(self.flag())
        self.assertIsNone(self.started[0]["start_flag_reason"])

    def test_first_seen_a_lap_after_the_gun_is_flagged(self):
        self.assertEqual(self.cross(GUN + 92), "started")
        self.assertEqual(self.flag(), "First seen 1:32 after the gun: start missed in the crowd, or joined late?")
        self.assertEqual(self.started[0]["start_flag_reason"], self.flag())
        flagged = self.db.get_flagged_starts()
        self.assertEqual([(f["bib_number"], f["seconds_after_gun"]) for f in flagged], [(7, 92.0)])

    def test_window(self):
        cases = [(30, False), (45, True), (599, True), (601, False)]  # no laps yet: up to 10 min
        for i, (offset, flagged) in enumerate(cases):
            with self.subTest(offset=offset):
                bib, epc = 100 + i, f"E2000000000000000000B{i:03d}"
                self.db.add_participant(bib, f"R{i}", epc)
                self.cross(GUN + offset, epc=epc)
                self.assertEqual(self.flag(bib) is not None, flagged)

    def test_window_follows_the_typical_lap_once_known(self):
        self.cross(GUN + 1, epc="E2000000000000000000AA02")
        for n in range(1, 6):
            self.db.record_lap(8, n, GUN + 1 + 100 * n, 100.0, -45.0)
        self.assertEqual(self.db.typical_lap_seconds(), 100.0)
        self.db.add_participant(9, "Late", "E2000000000000000000AA09")
        self.cross(GUN + 300, epc="E2000000000000000000AA09")   # 3 laps: joined late
        self.assertIsNone(self.flag(9))
        self.cross(GUN + 240)                                     # 2.4 laps: maybe missed
        self.assertIsNotNone(self.flag())

    def test_manual_start_is_not_flagged(self):
        self.cross(GUN + 90, force=True)
        self.assertIsNone(self.flag())

    def test_speed_multiplier(self):
        self.engine.speed_multiplier = 5.0  # mock mode: 18 real seconds = 90 race seconds
        self.cross(GUN + 18)
        self.assertIn("1:30", self.flag())

    def test_started_at_the_gun_adds_the_missing_first_lap(self):
        self.cross(GUN + 90)
        self.engine.on_lap_completed = None
        self.cross(GUN + 180)
        self.cross(GUN + 272)
        self.assertEqual([lap["lap_duration_seconds"] for lap in self.db.get_laps_for_runner(7)], [90.0, 92.0])

        result = self.db.start_from_gun(7)
        self.assertEqual(result, {"bib_number": 7, "start_time": GUN, "lap_seconds": 90.0})
        laps = self.db.get_laps_for_runner(7)
        self.assertEqual([(lap["lap_number"], lap["lap_duration_seconds"], lap["crossing_time"]) for lap in laps],
                         [(1, 90.0, GUN + 90), (2, 90.0, GUN + 180), (3, 92.0, GUN + 272)])
        p = self.db.get_participant_by_bib(7)
        self.assertEqual((p["start_time"], p["last_crossing_time"], p["start_flag_reason"]), (GUN, GUN + 272, None))
        self.assertEqual(self.db.get_leaderboard(bib_number=7)[0]["total_laps"], 3)
        self.assertEqual(self.db.get_flagged_starts(), [])
        self.cross(GUN + 362)  # timing carries on from the last crossing
        self.assertEqual(self.db.get_laps_for_runner(7)[-1]["lap_duration_seconds"], 90.0)

    def test_started_at_the_gun_before_any_lap(self):
        self.cross(GUN + 90)
        self.db.start_from_gun(7)
        p = self.db.get_participant_by_bib(7)
        self.assertEqual((p["start_time"], p["last_crossing_time"]), (GUN, GUN + 90))
        self.assertEqual(len(self.db.get_laps_for_runner(7)), 1)

    def test_start_from_gun_refuses_nonsense(self):
        with self.assertRaises(LapEditError):
            self.db.start_from_gun(7)  # not started
        self.cross(GUN + 2)
        with self.assertRaises(LapEditError):
            self.db.start_from_gun(7)  # 2 s after the gun is a start, not a missed lap
        self.assertEqual(self.db.get_laps_for_runner(7), [])

    def test_joined_late_keeps_the_start(self):
        self.cross(GUN + 90)
        self.db.keep_start(7)
        p = self.db.get_participant_by_bib(7)
        self.assertEqual((p["start_time"], p["start_flag_reason"]), (GUN + 90, None))
        self.assertEqual(self.db.get_flagged_starts(), [])

    def test_clear_results_drops_old_flags(self):  # issue #92
        self.cross(GUN + 200)
        self.assertIsNotNone(self.flag())
        self.db.reset_race_data()
        self.assertIsNone(self.flag())
        self.db.start_event(at=GUN + 5000)
        self.cross(GUN + 5001)  # clean start at the real gun
        self.assertIsNone(self.flag())
        self.assertEqual(self.db.get_flagged_starts(), [])

    def test_runner_reset_drops_old_flag(self):  # issue #92
        self.cross(GUN + 200)
        self.db.reset_participant_start(7)
        self.assertIsNone(self.flag())
        self.cross(GUN + 201, force=True)  # manual restart: never flagged
        self.assertEqual(self.db.get_flagged_starts(), [])


class MissedStartApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:
            raise unittest.SkipTest(str(e))
        os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "missed_start_import.db"))
        os.environ.setdefault("MOCK_HARDWARE", "true")
        from web import app as web_app
        cls.app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Database(os.path.join(cls.tmp.name, "api.db"))
        cls.patches = [mock.patch.object(web_app, "db", cls.db), mock.patch.object(web_app.admin_auth, "pin", None)]
        for p in cls.patches:
            p.start()
        cls.client = TestClient(web_app.app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def test_review_and_resolve(self):
        self.db.add_participant(21, "Kim", "E2000000000000000000CC21")
        self.db.add_participant(22, "Lee", "E2000000000000000000CC22")
        self.db.start_event(at=GUN)
        for bib in (21, 22):
            self.db.set_participant_start(bib, GUN + 95)
            self.db.flag_start(bib, "First seen 1:35 after the gun")
        self.assertEqual({f["bib_number"] for f in self.client.get("/api/starts/flagged").json()}, {21, 22})

        res = self.client.post("/api/participants/21/start-from-gun")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["lap_seconds"], 95.0)
        self.assertEqual(self.client.post("/api/participants/21/start-from-gun").status_code, 409)
        self.assertEqual(self.client.post("/api/participants/22/keep-start").status_code, 200)
        self.assertEqual(self.client.get("/api/starts/flagged").json(), [])
        board = {r["bib_number"]: r["total_laps"] for r in self.client.get("/api/leaderboard").json()}
        self.assertEqual((board[21], board[22]), (1, 0))

    def test_reset_start_forgets_the_tag_on_the_mat(self):  # issue #113
        self.db.add_participant(23, "Max", "E2000000000000000000CC23")
        self.db.start_event(at=GUN)
        self.db.set_participant_start(23, GUN + 5)
        with mock.patch.object(self.app.lap_engine, "forget_tag") as forget:
            self.assertEqual(self.client.post("/api/participants/23/reset").status_code, 200)
        forget.assert_called_once_with("E2000000000000000000CC23")
        self.assertIsNone(self.db.get_participant_by_bib(23)["start_time"])

    def test_admin_only(self):
        from web.auth import AdminAuth
        with mock.patch.object(self.app, "admin_auth", AdminAuth("482913")):
            self.assertEqual(self.client.get("/api/starts/flagged").status_code, 401)
            self.assertEqual(self.client.post("/api/participants/21/start-from-gun").status_code, 401)
            self.assertEqual(self.client.post("/api/participants/21/keep-start").status_code, 401)


if __name__ == "__main__":
    unittest.main()
