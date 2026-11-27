"""Tests for single per-participant goals (laps, distance, or time) and remaining-lap countdown."""
import asyncio
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from core.commentary import CommentaryEngine
from core.db import Database, RegistrationError
from core.goals import compute_goal_progress, normalize_goal


class TestGoalCalculations(unittest.TestCase):
    def test_normalize_goal_validation(self):
        self.assertEqual(normalize_goal(None, None), (None, None))
        self.assertEqual(normalize_goal("laps", 10), ("laps", 10.0))
        self.assertEqual(normalize_goal("distance", 5000), ("distance", 5000.0))
        self.assertEqual(normalize_goal("time", "30:00"), ("time", 1800.0))
        with self.assertRaises(ValueError):
            normalize_goal("speed", 10)
        with self.assertRaises(ValueError):
            normalize_goal("laps", 0)

    def test_lap_count_goal_countdown(self):
        p1 = compute_goal_progress("laps", 5, total_laps=2, total_time_seconds=200.0, track_length_m=400.0)
        self.assertEqual(p1["remaining_laps"], 3)
        self.assertEqual(p1["speech_text"], "3 laps to go!")
        self.assertFalse(p1["completed"])

        p4 = compute_goal_progress("laps", 5, total_laps=4, total_time_seconds=400.0, track_length_m=400.0)
        self.assertEqual(p4["remaining_laps"], 1)
        self.assertEqual(p4["speech_text"], "1 lap to go!")

        p5 = compute_goal_progress("laps", 5, total_laps=5, total_time_seconds=500.0, track_length_m=400.0)
        self.assertEqual(p5["remaining_laps"], 0)
        self.assertTrue(p5["completed"])
        self.assertTrue(p5["just_completed"])
        self.assertEqual(p5["speech_text"], "Goal complete!")

        # Bonus lap after goal doesn't re-announce "Goal complete!"
        p6 = compute_goal_progress("laps", 5, total_laps=6, total_time_seconds=600.0, track_length_m=400.0)
        self.assertTrue(p6["completed"])
        self.assertFalse(p6["just_completed"])
        self.assertIsNone(p6["speech_text"])

    def test_distance_goal_uses_400m_laps(self):
        # 5000m / 400m = 12.5 -> 13 laps to reach 5K
        p = compute_goal_progress("distance", 5000.0, total_laps=10, total_time_seconds=1000.0, track_length_m=400.0)
        self.assertEqual(p["target_laps"], 13)
        self.assertEqual(p["remaining_laps"], 3)
        self.assertEqual(p["label"], "5 km")
        self.assertEqual(p["speech_text"], "3 laps to go!")

    def test_time_goal_estimates_remaining_laps_from_pace(self):
        # 20 min goal (1200s), completed 5 laps in 600s (avg 120s/lap) -> 600s left -> 5 laps to go
        p = compute_goal_progress(
            "time", 1200.0, total_laps=5, total_time_seconds=600.0, track_length_m=400.0, last_lap_seconds=120.0
        )
        self.assertEqual(p["remaining_laps"], 5)
        self.assertEqual(p["speech_text"], "5 laps to go!")
        self.assertFalse(p["completed"])

        # Crossing 1200s on lap 10 -> goal complete!
        p_done = compute_goal_progress(
            "time", 1200.0, total_laps=10, total_time_seconds=1210.0, track_length_m=400.0, last_lap_seconds=120.0
        )
        self.assertEqual(p_done["remaining_laps"], 0)
        self.assertTrue(p_done["completed"])
        self.assertTrue(p_done["just_completed"])
        self.assertEqual(p_done["speech_text"], "Goal complete!")

    def test_commentary_includes_remaining_laps_speech(self):
        eng = CommentaryEngine(burn_frequency=0, seed=1)
        res = eng.for_lap(
            name="Elena",
            lap_number=10,
            lap_duration_seconds=95.0,
            goal_speech="3 laps to go!",
        )
        self.assertIn("3 laps to go!", res["speech"])


class TestParticipantGoalDBAndAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_single_goal_replaces_previous_goal_and_updates_leaderboard(self):
        self.db.add_participant(10, "Elena", "E28000000010", goal_type="distance", goal_value=5000.0)
        board = self.db.get_leaderboard(bib_number=10)
        self.assertEqual(board[0]["goal"]["target_laps"], 13)
        self.assertEqual(board[0]["goal"]["remaining_laps"], 13)

        # Setting a lap goal replaces the distance goal (only one goal per participant)
        self.db.set_participant_goal(10, "laps", 6)
        p = self.db.get_participant_by_bib(10)
        self.assertEqual(p["goal_type"], "laps")
        self.assertEqual(p["goal_value"], 6.0)

        with self.assertRaises(RegistrationError):
            self.db.set_participant_goal(10, "invalid", 5)

    def test_goal_patch_endpoint_and_on_lap_completed_countdown(self):
        import web.app as app_mod

        orig_db = app_mod.db
        try:
            app_mod.db = self.db
            client = TestClient(app_mod.app)
            self.assertEqual(
                client.post(
                    "/api/participants",
                    json={"bib_number": 21, "name": "Marcus", "goal_type": "distance", "goal_value": 1600},
                ).status_code,
                200,
            )
            # 1600m / 400m = 4 laps
            res_patch = client.patch("/api/participants/21/goal", json={"goal_type": "laps", "goal_value": 4})
            self.assertEqual(res_patch.status_code, 200)
            self.assertEqual(res_patch.json()["participant"]["goal_type"], "laps")

            # Simulate lap 1 recorded and check on_lap_completed attaches remaining_laps = 3
            self.db.start_event()
            t0 = self.db.get_event_state()["start_time"]
            self.db.set_participant_start(21, t0)
            lap_id = self.db.record_lap(21, 1, t0 + 90.0, 90.0, -45.0)
            lap_data = {
                "id": lap_id,
                "bib_number": 21,
                "name": "Marcus",
                "lap_number": 1,
                "lap_duration_seconds": 90.0,
                "crossing_time": t0 + 90.0,
            }
            asyncio.run(app_mod.on_lap_completed(lap_data))
            self.assertEqual(lap_data["goal"]["remaining_laps"], 3)
            self.assertIn("3 laps to go!", lap_data["commentary"]["speech"])
        finally:
            app_mod.db = orig_db


if __name__ == "__main__":
    unittest.main()
