"""HTTP-level tests. Needs httpx (pip install -r requirements-dev.txt)."""
import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ["DB_PATH"] = os.path.join(_tmp.name, "api_test.db")
os.environ["MOCK_HARDWARE"] = "true"
os.environ["SEED_DEMO"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from web import app as web_app  # noqa: E402

EPC_A = "E28011700000020101010001"
EPC_B = "E28011700000020101010002"


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(web_app.app)   # no lifespan: reader and flush loop stay off

    @classmethod
    def tearDownClass(cls):
        _tmp.cleanup()

    def setUp(self):
        # Fresh event per test so tests don't see each other's runners. A previous test may
        # have left the current event running, which would block switching away from it.
        if self.client.get("/api/event").json()["status"] == "running":
            self.client.post("/api/event/pause")
        res = self.client.post("/api/events", json={"name": f"Test {self._testMethodName}"})
        self.assertEqual(res.status_code, 200, res.text)
        self.event_id = res.json()["event"]["id"]

    def test_seed_flag_off_leaves_real_events_empty(self):
        self.assertEqual(self.client.get("/api/participants").json(), [])

    def test_end_button_flow(self):
        self.assertEqual(self.client.post("/api/event/end").status_code, 409, "can't end before starting")
        self.assertEqual(self.client.post("/api/event/start").status_code, 200)
        res = self.client.post("/api/event/end")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["event"]["status"], "ended")
        self.assertTrue(os.path.exists(res.json()["backup"]))
        self.assertEqual(self.client.post("/api/event/start").status_code, 409)
        self.assertEqual(self.client.post("/api/event/reopen").json()["event"]["status"], "paused")

    def test_reset_refused_while_running_and_backs_up_otherwise(self):
        self.client.post("/api/event/start")
        self.assertEqual(self.client.post("/api/event/reset").status_code, 409)
        self.client.post("/api/event/pause")
        res = self.client.post("/api/event/reset")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(os.path.exists(res.json()["backup"]))

    def test_cannot_switch_events_while_running(self):
        other = self.client.get("/api/events").json()[-1]["id"]  # oldest event
        self.client.post("/api/event/start")
        self.assertEqual(self.client.post(f"/api/events/{other}/select").status_code, 409)

    def test_import_then_assign_tags(self):
        csv_text = "Name,Email\nPat Runner,pat@example.com\nSam Sprinter,sam@example.com\n"
        res = self.client.post("/api/participants/import", json={"csv_text": csv_text})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual([i["bib"] for i in res.json()["imported"]], [1, 2])

        ok = self.client.put("/api/participants/1/tag", json={"epc": EPC_A})
        self.assertEqual(ok.status_code, 200)
        clash = self.client.put("/api/participants/2/tag", json={"epc": EPC_A})
        self.assertEqual(clash.status_code, 400)
        self.assertIn("already assigned to #1 Pat Runner", clash.json()["detail"])

        self.assertEqual(self.client.put("/api/participants/2/tag", json={"epc": EPC_B}).status_code, 200)
        self.assertEqual(self.client.delete("/api/participants/2/tag").status_code, 200)
        tags = {p["bib_number"]: p["epc"] for p in self.client.get("/api/participants").json()}
        self.assertEqual(tags, {1: EPC_A, 2: None})

    def test_import_without_name_column_is_400(self):
        res = self.client.post("/api/participants/import", json={"csv_text": "Email\nx@example.com\n"})
        self.assertEqual(res.status_code, 400)

    def test_register_without_tag_and_csv_downloads(self):
        res = self.client.post("/api/participants", json={"bib_number": 5, "name": "Pat", "email": "p@example.com"})
        self.assertEqual(res.status_code, 200, res.text)
        for kind in ("results", "laps", "registrations", "raw_reads"):
            r = self.client.get(f"/api/events/{self.event_id}/{kind}.csv")
            self.assertEqual(r.status_code, 200, kind)
            self.assertIn("text/csv", r.headers["content-type"])
            self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("Pat", self.client.get(f"/api/events/{self.event_id}/registrations.csv").text)
        self.assertEqual(self.client.get("/api/events/9999/results.csv").status_code, 404)

    def test_pages_render_and_escape_names(self):
        evil = "</script><script>alert(1)</script>"
        self.client.post("/api/participants", json={"bib_number": 9, "name": evil})
        for path in ("/crossings", "/leaderboard", "/admin", "/prepare", "/runner/9"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn(evil, r.text, f"{path} must not emit the name unescaped")

    def test_quick_lap_adjustments_and_event_isolation(self):
        from unittest import mock
        t0 = 1_000_000.0
        at = lambda t: mock.patch("core.clock.now", return_value=t)  # noqa: E731
        self.client.post("/api/participants", json={"bib_number": 10, "name": "Alex"})
        with at(t0):
            self.client.post("/api/event/start")
            # Not started yet: quick-add records the start rather than inventing a lap (#9)
            r0 = self.client.post("/api/participants/10/quick-add-lap")
        self.assertEqual(r0.status_code, 200, r0.text)
        self.assertEqual(r0.json()["status"], "started")

        # Lap 1 is timed from the start
        with at(t0 + 70):
            r1 = self.client.post("/api/participants/10/quick-add-lap")
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r1.json()["lap"]["lap_number"], 1)
        self.assertEqual(r1.json()["lap"]["lap_duration_seconds"], 70.0)
        p_after_1 = {p["bib_number"]: p for p in self.client.get("/api/participants").json()}[10]
        self.assertEqual(p_after_1["last_crossing_time"], t0 + 70)

        # A double tap right after is rejected (under the event's minimum lap)
        with at(t0 + 75):
            self.assertEqual(self.client.post("/api/participants/10/quick-add-lap").status_code, 409)

        # An explicit lap time ends that long after the previous crossing
        with at(t0 + 200):
            r2 = self.client.post("/api/participants/10/quick-add-lap", json={"lap_duration_seconds": 62.5})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["lap"]["lap_number"], 2)
        self.assertEqual(r2.json()["lap"]["lap_duration_seconds"], 62.5)
        self.assertEqual(r2.json()["lap"]["crossing_time"], t0 + 132.5)

        # Ending the event freezes quick lap adjustments
        self.client.post("/api/event/end")
        self.assertEqual(self.client.post("/api/participants/10/quick-add-lap").status_code, 409)
        self.assertEqual(self.client.post("/api/participants/10/quick-remove-lap").status_code, 409)

        # Reopening allows removing laps cleanly
        self.client.post("/api/event/reopen")
        rem1 = self.client.post("/api/participants/10/quick-remove-lap")
        self.assertEqual(rem1.status_code, 200, rem1.text)
        self.assertEqual(rem1.json()["removed_lap"]["lap_number"], 2)
        p_after_remove = {p["bib_number"]: p for p in self.client.get("/api/participants").json()}[10]
        self.assertEqual(p_after_remove["last_crossing_time"], t0 + 70, "back to lap 1's crossing")

        # Switching to a new event isolates laps so removing bib 10 in event A doesn't touch event B
        other_res = self.client.post("/api/events", json={"name": "Second Event"})
        self.assertEqual(other_res.status_code, 200)
        self.client.post("/api/participants", json={"bib_number": 10, "name": "Alex Again"})
        self.assertEqual(self.client.post("/api/participants/10/quick-remove-lap").status_code, 400)
        self.assertEqual(self.client.post("/api/participants/999/quick-remove-lap").status_code, 404)
        # First event still has 1 lap recorded
        self.assertIn("Alex", self.client.get(f"/api/events/{self.event_id}/laps.csv").text)

    def test_simulator_playback_and_scaled_speed(self):
        state = self.client.get("/api/simulator/state")
        self.assertEqual(state.status_code, 200)
        body = state.json()
        self.assertEqual(len(body["runners"]), 5)
        self.assertFalse(body["paused"])

        paused = self.client.post("/api/simulator/pause")
        self.assertEqual(paused.status_code, 200)
        self.assertTrue(paused.json()["sim"]["paused"])

        speed = self.client.post("/api/simulator/speed", json={"speed": 20.0})
        self.assertEqual(speed.status_code, 200)
        self.assertEqual(speed.json()["speed"], 20.0)

        resumed = self.client.post("/api/simulator/play")
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["sim"]["paused"])
        self.assertEqual(resumed.json()["sim"]["speed"], 20.0)

    def test_self_register_unassigned_tag(self):
        # Auto-assigns next available bib when bib_number is omitted
        r1 = self.client.post("/api/self-register", json={
            "name": "Sam Walkup",
            "epc": "E28099990000000000000042",
        })
        self.assertEqual(r1.status_code, 200, r1.text)
        body1 = r1.json()
        self.assertEqual(body1["status"], "created")
        self.assertGreaterEqual(body1["bib_number"], 1)
        self.assertEqual(body1["participant"]["name"], "Sam Walkup")
        self.assertEqual(body1["participant"]["epc"], "E28099990000000000000042")

        # Duplicate EPC is rejected
        dup = self.client.post("/api/self-register", json={
            "name": "Duplicate Runner",
            "epc": "E28099990000000000000042",
        })
        self.assertEqual(dup.status_code, 400)

        # Explicit bib_number is honored
        r2 = self.client.post("/api/self-register", json={
            "name": "Explicit Bib",
            "epc": "E28099990000000000000043",
            "bib_number": 99,
        })
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["bib_number"], 99)

    def test_full_event_reset_clears_participants_and_laps(self):
        self.client.post("/api/participants", json={"bib_number": 50, "name": "Temp Runner", "epc": "E28000000000000000000050"})
        self.client.post("/api/event/start")
        self.client.post("/api/participants/50/quick-add-lap")

        # Cannot reset while running
        running_res = self.client.post("/api/event/reset-all")
        self.assertEqual(running_res.status_code, 409)

        # Pause, then full reset clears participants, laps, and returns event to setup
        self.client.post("/api/event/pause")
        res = self.client.post("/api/event/reset-all")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["status"], "reset_all")
        self.assertEqual(self.client.get("/api/participants").json(), [])
        self.assertEqual(self.client.get("/api/event").json()["status"], "setup")


if __name__ == "__main__":
    unittest.main()
