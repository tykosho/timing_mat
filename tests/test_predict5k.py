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

    def test_full_club_predict_5k_simulation(self):
        """End-to-end 6-runner Predict 5K simulation: registration, countdown,
        mixed RFID/AprilTag/manual finishes, 1s rounding, 2-way tie, no spoilers,
        worst-to-first reveal prepending to top of board, and reset."""
        import web.app as app_mod
        from core.db import Database

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        orig_db = app_mod.db
        orig_now = clock.now
        sim_now = 5_000_000.0
        try:
            clock.now = lambda: sim_now
            app_mod.db = Database(tmp.name)
            client = TestClient(app_mod.app)

            # Reset any residual session in this fresh DB and configure race
            self.assertEqual(
                client.post("/api/predict/settings", json={"name": "Thursday Predict 5K", "countdown_seconds": 3, "min_finish_seconds": 5}).status_code,
                200,
            )

            runners = [
                {"name": "Elena",  "predicted_time": "22:00", "epc": "E28000000101"},
                {"name": "Marcus", "predicted_time": "24:30", "apriltag_id": 3},
                {"name": "Sam",    "predicted_time": "25:15", "epc": "E28000000103", "apriltag_id": 7},
                {"name": "Dana",   "predicted_time": "21:45", "apriltag_id": 12},
                {"name": "Priya",  "predicted_time": "26:00"},
                {"name": "Alex",   "predicted_time": "23:10", "epc": "E28000000106"},
            ]
            ids = {}
            for r in runners:
                res = client.post("/api/predict/entries", json=r)
                self.assertEqual(res.status_code, 200, res.text)
                ids[r["name"]] = res.json()["entry"]["id"]

            # 1. No spoilers in registration state
            st = client.get("/api/predict/state").json()
            self.assertEqual(st["session"]["total_entries"], 6)
            for e in st["entries"]:
                self.assertEqual(e["predicted_formatted"], "Locked")
                self.assertNotIn("predicted_seconds", e)

            # 1b. Update a runner's prediction before race start (still stays Locked)
            upd = client.patch(f"/api/predict/entries/{ids['Dana']}/prediction", json={"predicted_time": "21:40"})
            self.assertEqual(upd.status_code, 200)
            # Adjust Dana's finish time to +4s from new 21:40 guess (21:44)
            dana_target_sec = 21 * 60 + 44.4

            # 2. Start race with 3s countdown -> countdown state -> advance clock past gun
            st = client.post("/api/predict/start", json={"countdown_seconds": 3}).json()["state"]
            self.assertEqual(st["session"]["status"], "countdown")
            gun_time = st["session"]["gun_time"]
            sim_now = gun_time + 0.1
            st = client.get("/api/predict/state").json()
            self.assertEqual(st["session"]["status"], "running")

            # Editing prediction after race start must be rejected
            self.assertEqual(
                client.patch(f"/api/predict/entries/{ids['Dana']}/prediction", json={"predicted_time": "20:00"}).status_code,
                400,
            )

            # 3. False early crossing (< min_finish_seconds=5) is ignored
            self.assertIsNone(
                app_mod.predict_5k.process_tag_read(
                    TagRead(epc="E28000000101", timestamp=gun_time + 2.0, rssi=-45.0, source="rfid")
                )
            )

            # 4. Simulate finishes with mixed sensors & 1-second rounding
            # Priya: +45s (rank 6, manual)
            sim_now = gun_time + 26 * 60 + 45.2
            self.assertEqual(client.post(f"/api/predict/entries/{ids['Priya']}/finish").status_code, 200)

            # Elena: -18s (rank 5, RFID)
            app_mod.predict_5k.process_tag_read(
                TagRead(epc="E28000000101", timestamp=gun_time + 21 * 60 + 42.1, rssi=-44.0, source="rfid")
            )
            # Alex: +9s (rank 4, RFID)
            app_mod.predict_5k.process_tag_read(
                TagRead(epc="E28000000106", timestamp=gun_time + 23 * 60 + 18.8, rssi=-46.0, source="rfid")
            )
            # Dana: +4s (rank 3, AprilTag)
            app_mod.predict_5k.process_tag_read(
                TagRead(epc=apriltag_to_epc(12), timestamp=gun_time + dana_target_sec, rssi=-41.0, source="apriltag", apriltag_id=12)
            )
            # Marcus: -2s (rank 1 TIE, AprilTag) -> 24:28.3 rounds to 24:28 (-2s)
            app_mod.predict_5k.process_tag_read(
                TagRead(epc=apriltag_to_epc(3), timestamp=gun_time + 24 * 60 + 28.3, rssi=-40.0, source="apriltag", apriltag_id=3)
            )
            # Sam: +2s (rank 1 TIE, AprilTag) -> 25:17.2 rounds to 25:17 (+2s)
            app_mod.predict_5k.process_tag_read(
                TagRead(epc=apriltag_to_epc(7), timestamp=gun_time + 25 * 60 + 17.2, rssi=-40.0, source="apriltag", apriltag_id=7)
            )
            # Sam's RFID tag crossing 1s later must be ignored (no double-finish)
            self.assertIsNone(
                app_mod.predict_5k.process_tag_read(
                    TagRead(epc="E28000000103", timestamp=gun_time + 25 * 60 + 18.2, rssi=-43.0, source="rfid")
                )
            )

            # 5. Still NO spoilers while running
            st = client.get("/api/predict/state").json()
            self.assertEqual(st["session"]["total_finishers"], 6)
            self.assertEqual(st["revealed_board"], [])
            for e in st["entries"]:
                self.assertTrue(e["finished"])
                self.assertEqual(e["predicted_formatted"], "Locked")

            # 6. Worst-to-first Reveal Sequence
            self.assertEqual(client.post("/api/predict/reveal/start").status_code, 200)
            revealed_order = []
            for step in range(6):
                res = client.post("/api/predict/reveal/next").json()["state"]
                latest = res["latest_revealed"]
                revealed_order.append((latest["name"], latest["rank"], latest["abs_delta_seconds"]))
                # Newest revealed is always at index 0 (top of Results Board)
                self.assertEqual(res["revealed_board"][0]["name"], latest["name"])
                self.assertEqual(len(res["revealed_board"]), step + 1)

            self.assertEqual(
                [x[0] for x in revealed_order[:4]],
                ["Priya", "Elena", "Alex", "Dana"],
            )
            self.assertEqual([x[1] for x in revealed_order[:4]], [6, 5, 4, 3])
            # Top 2 are tied co-winners at rank 1, 2s off
            self.assertEqual({x[0] for x in revealed_order[4:]}, {"Marcus", "Sam"})
            self.assertEqual([x[1] for x in revealed_order[4:]], [1, 1])
            self.assertEqual([x[2] for x in revealed_order[4:]], [2, 2])
            self.assertEqual(res["session"]["status"], "ended")
            self.assertIn("Co-winner", res["latest_revealed"]["speech"])

            # 7. Reset clears finishes & board, keeps roster
            reset_st = client.post("/api/predict/reset").json()["state"]
            self.assertEqual(reset_st["session"]["status"], "setup")
            self.assertEqual(reset_st["session"]["total_finishers"], 0)
            self.assertEqual(reset_st["session"]["total_entries"], 6)
            self.assertEqual(reset_st["revealed_board"], [])
        finally:
            clock.now = orig_now
            app_mod.db = orig_db
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
