"""Tests for Predict 5K mode: registration, spoiler-free finish capture, 1s ties, and worst-to-first reveal."""
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from core import clock
from core.models import TagRead, apriltag_to_epc
from core.predict5k import Predict5KManager, format_mmss_int, parse_predicted_time


class TestPredict5KManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.mgr = Predict5KManager(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_parse_predicted_time(self):
        self.assertEqual(parse_predicted_time("24:30"), 1470)
        self.assertEqual(parse_predicted_time("1:02:05"), 3725)
        self.assertEqual(parse_predicted_time("1200"), 1200)
        self.assertEqual(format_mmss_int(1470), "24:30")
        with self.assertRaises(ValueError):
            parse_predicted_time("24:75")

    def test_no_spoilers_during_run_and_worst_to_first_reveal_with_ties(self):
        self.mgr.update_settings(countdown_seconds=0, min_finish_seconds=5)
        e_slow = self.mgr.add_entry("Alice", "20:00", epc="E28000000001")   # 1200s
        self.mgr.add_entry("Bob", "20:00", apriltag_id=4)                  # 1200s
        e_tie2 = self.mgr.add_entry("Cara", "21:00", apriltag_id=5)        # 1260s

        # Even during setup, roster entries must NEVER expose predicted times
        setup_state = self.mgr.get_state()
        for entry in setup_state["entries"]:
            self.assertEqual(entry["predicted_formatted"], "Locked")
            self.assertNotIn("predicted_seconds", entry)

        state = self.mgr.start_race(countdown_override=0)
        self.assertEqual(state["session"]["status"], "running")
        t0 = state["session"]["start_time"]

        # Too-soon read (< min_finish_seconds) must be ignored
        self.assertIsNone(
            self.mgr.process_tag_read(TagRead(epc="E28000000001", timestamp=t0 + 2.0, rssi=-45.0, source="rfid"))
        )

        # Alice finishes +20s off (1220.4s -> rounds to 1220s -> +20s)
        fin_a = self.mgr.process_tag_read(
            TagRead(epc="E28000000001", timestamp=t0 + 1220.4, rssi=-45.0, source="rfid")
        )
        self.assertEqual(fin_a["name"], "Alice")

        # Bob finishes -3.2s off (1196.8s -> rounds to 1197s -> -3s)
        fin_b = self.mgr.process_tag_read(
            TagRead(epc=apriltag_to_epc(4), timestamp=t0 + 1196.8, rssi=-42.0, source="apriltag", apriltag_id=4)
        )
        self.assertEqual(fin_b["name"], "Bob")

        # Cara finishes +3.1s off (1263.1s -> rounds to 1263s -> +3s) -> TIED for 1st with Bob!
        fin_c = self.mgr.record_finish(e_tie2["id"], finish_time=t0 + 1263.1, source="manual")
        self.assertEqual(fin_c["name"], "Cara")

        # Duplicate crossing by Alice must be ignored
        self.assertIsNone(
            self.mgr.process_tag_read(TagRead(epc="E28000000001", timestamp=t0 + 1250.0, rssi=-45.0, source="rfid"))
        )

        # While running, NO spoilers in state
        running_state = self.mgr.get_state()
        self.assertEqual(running_state["revealed_board"], [])
        for entry in running_state["entries"]:
            self.assertEqual(entry["predicted_formatted"], "Locked")
            self.assertNotIn("actual_seconds", entry)

        # Reveal #1 -> worst delta first (Alice, +20s)
        self.mgr.start_reveal()
        s1 = self.mgr.reveal_next()
        self.assertEqual(s1["latest_revealed"]["name"], "Alice")
        self.assertEqual(s1["latest_revealed"]["rank"], 3)
        self.assertEqual([r["name"] for r in s1["revealed_board"]], ["Alice"])

        # Reveal #2 -> Cara (+3s, rank 1 tie) prepended to TOP of board
        s2 = self.mgr.reveal_next()
        self.assertEqual(s2["latest_revealed"]["name"], "Cara")
        self.assertEqual(s2["latest_revealed"]["rank"], 1)
        self.assertTrue(s2["latest_revealed"]["is_tie"])
        self.assertTrue(s2["latest_revealed"]["is_winner"])
        self.assertEqual([r["name"] for r in s2["revealed_board"]], ["Cara", "Alice"])

        # Reveal #3 -> Bob (-3s, rank 1 tie) prepended to TOP of board -> status becomes 'ended'
        s3 = self.mgr.reveal_next()
        self.assertEqual(s3["session"]["status"], "ended")
        self.assertEqual(s3["latest_revealed"]["name"], "Bob")
        self.assertEqual(s3["latest_revealed"]["rank"], 1)
        self.assertTrue(s3["latest_revealed"]["is_winner"])
        self.assertEqual([r["name"] for r in s3["revealed_board"]], ["Bob", "Cara", "Alice"])
        self.assertEqual(e_slow["id"], s3["entries"][0]["id"])

    def test_countdown_promotes_to_running(self):
        self.mgr.add_entry("Sam", "25:00", apriltag_id=1)
        st = self.mgr.start_race(countdown_override=2)
        self.assertEqual(st["session"]["status"], "countdown")
        # Simulate clock passing gun_time
        orig_now = clock.now
        try:
            gun = st["session"]["gun_time"]
            clock.now = lambda: gun + 0.5
            st2 = self.mgr.get_state()
            self.assertEqual(st2["session"]["status"], "running")
            self.assertEqual(st2["session"]["start_time"], gun)
        finally:
            clock.now = orig_now


class TestPredict5KWebAPI(unittest.TestCase):
    def test_predict_page_and_api_flow(self):
        import web.app as app_mod
        from core.db import Database

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        orig_db = app_mod.db
        try:
            app_mod.db = Database(tmp.name)
            client = TestClient(app_mod.app)
            self.assertEqual(client.get("/predict").status_code, 200)
            r = client.get("/api/predict/state")
            self.assertEqual(r.status_code, 200)
            self.assertIn("session", r.json())

            r_add = client.post("/api/predict/entries", json={"name": "Dana", "predicted_time": "22:15", "apriltag_id": 9})
            self.assertEqual(r_add.status_code, 200)
            entry_id = r_add.json()["entry"]["id"]

            r_del = client.delete(f"/api/predict/entries/{entry_id}")
            self.assertEqual(r_del.status_code, 200)
        finally:
            app_mod.db = orig_db
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
