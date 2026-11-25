"""HTTP-level tests. Needs httpx (pip install -r requirements-dev.txt)."""
import os
import tempfile
import unittest
from unittest import mock

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

    def test_elapsed_time_holds_while_paused(self):
        self.client.post("/api/event/start")
        self.client.post("/api/event/pause")
        paused_at = self.client.get("/api/event").json()["paused_at"]
        with mock.patch("core.clock.now", return_value=paused_at + 100.0):   # a long hold
            ev = self.client.get("/api/event").json()
        self.assertEqual(ev["elapsed_seconds"], round(ev["paused_at"] - ev["start_time"], 1))

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

    def test_csv_exports_run_off_the_event_loop(self):
        """A raw_reads.csv of a whole race takes seconds to build: it must not freeze every
        screen and the lap engine meanwhile (issue #94)."""
        import asyncio
        from unittest import mock
        on_loop = {}

        def probe(kind):
            def build(db, event_id):
                try:
                    asyncio.get_running_loop()
                    on_loop[kind] = True
                except RuntimeError:
                    on_loop[kind] = False
                return "a,b\n"
            return build

        names = {"results": "results_csv", "laps": "laps_csv",
                 "registrations": "registrations_csv", "raw_reads": "raw_reads_csv"}
        for kind, name in names.items():
            with mock.patch.object(web_app.exports, name, probe(kind)):
                r = self.client.get(f"/api/events/{self.event_id}/{kind}.csv")
            self.assertEqual((r.status_code, r.text), (200, "a,b\n"), kind)
        self.assertEqual(on_loop, {kind: False for kind in names})

    def test_pages_render_and_escape_names(self):
        evil = "</script><script>alert(1)</script>"
        self.client.post("/api/participants", json={"bib_number": 9, "name": evil})
        for path in ("/crossings", "/leaderboard", "/admin", "/prepare", "/runner/9"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn(evil, r.text, f"{path} must not emit the name unescaped")

    def test_break_buttons_and_open_break_on_runner_page(self):
        self.client.post("/api/participants", json={"bib_number": 5, "name": "Pat", "epc": EPC_A})
        self.client.post("/api/event/start")
        self.client.post("/api/participants/5/start")
        res = self.client.post("/api/participants/5/break/start")
        self.assertEqual(res.status_code, 200, res.text)
        page = self.client.get("/runner/5").text
        self.assertIn("In progress", page, "an open break has no end time yet (#86)")
        self.assertNotIn("new Date(0.0 * 1000)", page)
        res = self.client.post("/api/participants/5/break/end")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["status"], "resumed")
        self.assertNotIn("In progress", self.client.get("/runner/5").text)

    def test_runner_page_pace_uses_the_event_track_length(self):
        """#95: pace per mile came from a hardcoded 400 m lap."""
        from unittest import mock
        t0 = 1_000_000.0
        at = lambda t: mock.patch("core.clock.now", return_value=t)  # noqa: E731
        self.client.post("/api/event/settings", json={"track_length_m": 200})
        self.client.post("/api/participants", json={"bib_number": 11, "name": "Sam"})
        with at(t0):
            self.client.post("/api/event/start")
            self.client.post("/api/participants/11/quick-add-lap")
        with at(t0 + 60):
            self.client.post("/api/participants/11/quick-add-lap")
        page = self.client.get("/runner/11").text
        self.assertIn("8:02 /mi", page, "60 s per 200 m is 8:02 per mile")
        self.assertNotIn("4:01 /mi", page)

    def test_init_and_event_status_carry_server_time(self):
        """#95: open pages re-derive their clock offset from these, after the Pi's clock resyncs."""
        from unittest import mock
        with mock.patch("core.clock.now", return_value=1_234_567.25):
            with self.client.websocket_connect("/ws/live") as ws:
                self.assertEqual(ws.receive_json()["server_now"], 1_234_567.25)
            with mock.patch.object(web_app, "broadcast_ws") as broadcast:
                self.client.post("/api/event/settings", json={"min_lap_seconds": 30})
        sent = broadcast.call_args.args[0]
        self.assertEqual(sent["type"], "event_status")
        self.assertEqual(sent["server_now"], 1_234_567.25)
        for name in ("leaderboard.html", "admin.html", "crossings.html"):
            with open(os.path.join("web", "templates", name)) as f:
                self.assertIn("msg.server_now", f.read(), name)

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

    def test_update_settings_allow_disabling_auto_break(self):
        res = self.client.post("/api/event/settings", json={"max_lap_rest_seconds": 0.0})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["event"]["max_lap_rest_seconds"], 0.0)

    def test_self_register_mid_race_starts_runner_and_counts_lap_1(self):
        import asyncio
        from unittest import mock
        from core.models import TagRead
        from web import app as web_app

        self.client.post("/api/simulator/speed", json={"speed": 1.0})
        t0 = 2_000_000.0
        epc = "E28099990000000000000122"
        at = lambda t: mock.patch("core.clock.now", return_value=t)  # noqa: E731

        with at(t0):
            self.client.post("/api/event/start")

        # Runner steps onto mat at t0 + 120s and stands typing until t0 + 135s
        web_app.lap_engine.process_tag_read(TagRead(epc=epc, rssi=-44.0, timestamp=t0 + 130.0))
        with at(t0 + 135.0):
            reg = self.client.post("/api/self-register", json={
                "name": "Late Walkup",
                "epc": epc,
                "crossing_time": t0 + 120.0,
                "peak_rssi": -43.0,
            })
        self.assertEqual(reg.status_code, 200, reg.text)
        body = reg.json()
        self.assertTrue(body["started"])
        self.assertEqual(body["start_time"], t0 + 120.0)
        bib = body["bib_number"]

        # Walk-up start is intentional, never flagged as a missed gun start
        self.assertEqual(self.client.get("/api/starts/flagged").json(), [])

        # Stepping off the mat 1s later does NOT trigger a too-soon lap or prompt
        web_app.lap_engine.process_tag_read(TagRead(epc=epc, rssi=-45.0, timestamp=t0 + 136.0))
        asyncio.run(web_app.lap_engine.flush_once(t0 + 140.0))
        self.assertEqual(web_app.db.get_laps_for_runner(bib), [])

        # Completing their first loop at t0 + 210s records Lap 1 (90s), not Lap 0!
        web_app.lap_engine.process_tag_read(TagRead(epc=epc, rssi=-42.0, timestamp=t0 + 210.0))
        asyncio.run(web_app.lap_engine.flush_once(t0 + 215.0))
        laps = web_app.db.get_laps_for_runner(bib)
        self.assertEqual(len(laps), 1)
        self.assertEqual(laps[0]["lap_number"], 1)
        self.assertAlmostEqual(laps[0]["lap_duration_seconds"], 90.0, places=1)

    def test_auto_placeholder_for_unknown_tag_and_inline_rename(self):
        import asyncio
        from unittest import mock
        from core.models import TagRead
        from web import app as web_app

        self.client.post("/api/simulator/speed", json={"speed": 1.0})
        t0 = 3_000_000.0
        epc_a = "E28099990000000000000201"
        epc_b = "E28099990000000000000202"
        with mock.patch("core.clock.now", return_value=t0):
            self.client.post("/api/event/start")

        # Two unknown tags cross together without anyone typing at the prompt yet
        web_app.lap_engine.process_tag_read(TagRead(epc=epc_a, rssi=-43.0, timestamp=t0 + 10.0))
        web_app.lap_engine.process_tag_read(TagRead(epc=epc_b, rssi=-44.0, timestamp=t0 + 10.2))
        asyncio.run(web_app.lap_engine.flush_once(t0 + 15.0))

        pa = web_app.db.get_participant_by_epc(epc_a)
        pb = web_app.db.get_participant_by_epc(epc_b)
        self.assertIsNotNone(pa)
        self.assertIsNotNone(pb)
        self.assertEqual(pa["name"], f"Runner #{pa['bib_number']}")
        self.assertEqual(pb["name"], f"Runner #{pb['bib_number']}")
        # Placeholders are not timed until they have a name
        self.assertIsNone(pa["start_time"])
        board = {r["bib_number"]: r for r in self.client.get("/api/leaderboard").json()}
        self.assertTrue(board[pa["bib_number"]]["unclaimed"])

        # Runner A crosses again a lap later, still unnamed: no lap, still not started
        web_app.lap_engine.process_tag_read(TagRead(epc=epc_a, rssi=-42.0, timestamp=t0 + 95.0))
        asyncio.run(web_app.lap_engine.flush_once(t0 + 100.0))
        self.assertEqual(web_app.db.get_laps_for_runner(pa["bib_number"]), [])
        self.assertIsNone(web_app.db.get_participant_by_bib(pa["bib_number"])["start_time"])
        replay_bibs = {r["bib"] for r in self.client.get("/api/replay").json()["runners"]}
        self.assertNotIn(pa["bib_number"], replay_bibs)

        # Runner A names themself from the pill (PATCH): their clock starts now
        with mock.patch("core.clock.now", return_value=t0 + 100.0):
            ren = self.client.patch(f"/api/participants/{pa['bib_number']}/name", json={"name": "Jordan Lee"})
        self.assertEqual(ren.status_code, 200, ren.text)
        self.assertEqual(ren.json()["participant"]["name"], "Jordan Lee")
        self.assertEqual(ren.json()["participant"]["notes"], "")
        self.assertEqual(ren.json()["participant"]["start_time"], t0 + 100.0)
        board = {r["bib_number"]: r for r in self.client.get("/api/leaderboard").json()}
        self.assertFalse(board[pa["bib_number"]]["unclaimed"])

        # Their next crossing is Lap 1, timed from when they were named
        web_app.lap_engine.process_tag_read(TagRead(epc=epc_a, rssi=-42.0, timestamp=t0 + 190.0))
        asyncio.run(web_app.lap_engine.flush_once(t0 + 195.0))
        laps_a = web_app.db.get_laps_for_runner(pa["bib_number"])
        self.assertEqual(len(laps_a), 1)
        self.assertAlmostEqual(laps_a[0]["lap_duration_seconds"], 90.0, places=1)

        # Runner B submits via the prompt (POST /api/self-register) -> renames placeholder and starts
        with mock.patch("core.clock.now", return_value=t0 + 200.0):
            reg_b = self.client.post("/api/self-register", json={"name": "Casey Kim", "epc": epc_b})
        self.assertEqual(reg_b.status_code, 200, reg_b.text)
        self.assertEqual(reg_b.json()["bib_number"], pb["bib_number"])
        self.assertEqual(reg_b.json()["participant"]["name"], "Casey Kim")
        self.assertTrue(reg_b.json()["started"])
        self.assertEqual(reg_b.json()["start_time"], t0 + 200.0)

    def test_naming_a_placeholder_from_admin_clears_the_walk_up_note(self):
        from core.db import WALKUP_NOTE
        from web import app as web_app

        p = web_app.db.add_placeholder_participant("E28099990000000000000203")
        res = self.client.put(f"/api/participants/{p['bib_number']}", json={
            "bib_number": p["bib_number"], "name": "Sam Park", "epc": p["epc"], "notes": WALKUP_NOTE,
        })
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(web_app.db.get_participant_by_bib(p["bib_number"])["notes"], "")

if __name__ == "__main__":
    unittest.main()
