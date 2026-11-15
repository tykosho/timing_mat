"""Forced (admin/simulator) crossings must never record an invented lap time. See issue #4."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

# Same setup as test_api.py, in case this module is the first to import web.app. The API tests
# below still swap in their own Database, because web.app is shared across test modules and
# test_api.py deletes its DB when it finishes.
_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("DB_PATH", os.path.join(_tmp.name, "manual_crossing_import.db"))
os.environ.setdefault("MOCK_HARDWARE", "true")
os.environ.setdefault("SEED_DEMO", "false")

from core.db import Database
from core.lap_engine import LapEngine

EPC_101 = "E28011700000020101010001"


class ForcedCrossingEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "race.db"))
        self.db.seed_demo_runners()
        self.db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=300.0)
        self.db.start_event(at=1000.0)
        self.laps = []
        self.engine = LapEngine(db=self.db, crossing_window=0.5, on_lap_completed=self.laps.append)
        # Chip start for bib 101 at t=1100
        self.assertEqual(self.run_crossing(1100.0), "started")

    def tearDown(self):
        self.tmp.cleanup()

    def run_crossing(self, t, force=True):
        return asyncio.run(self.engine._resolve_crossing(EPC_101, [(t, -42.0)], force=force))

    def test_forced_crossing_too_soon_is_rejected_not_invented(self):
        # 10 s after the start: previously this recorded random.uniform(62, 85)
        self.assertEqual(self.run_crossing(1110.0), "debounced")
        self.assertEqual(self.laps, [])
        self.assertEqual(self.db.get_laps_for_runner(101), [])
        # The rejected crossing must not move the runner's last crossing either
        self.assertEqual(self.db.get_participant_by_bib(101)["last_crossing_time"], 1100.0)

    def test_forced_crossing_uses_real_elapsed_time(self):
        self.assertEqual(self.run_crossing(1172.5), "lap")
        laps = self.db.get_laps_for_runner(101)
        self.assertEqual(len(laps), 1)
        self.assertEqual(laps[0]["lap_duration_seconds"], 72.5)
        self.assertEqual(laps[0]["manual_entry"], 1)

    def test_forced_crossing_is_repeatable(self):
        # Same inputs, same result: nothing random left in the forced path
        self.assertEqual(self.run_crossing(1160.0), "lap")
        self.assertEqual(self.run_crossing(1220.0), "lap")
        durations = [l["lap_duration_seconds"] for l in self.db.get_laps_for_runner(101)]
        self.assertEqual(durations, [60.0, 60.0])

    def test_forced_crossing_scales_with_simulator_speed(self):
        # At 4x, 15 real seconds is a 60 s simulated lap, and the 45 s minimum becomes 11.25 s
        self.engine.speed_multiplier = 4.0
        self.assertEqual(self.run_crossing(1105.0), "debounced")
        self.assertEqual(self.run_crossing(1115.0), "lap")
        self.assertEqual(self.db.get_laps_for_runner(101)[0]["lap_duration_seconds"], 60.0)

    def test_trigger_manual_crossing_reports_outcome(self):
        with mock.patch("core.lap_engine.time.time", return_value=1105.0):
            result = asyncio.run(self.engine.trigger_manual_crossing(101))
        self.assertEqual(result["participant"]["bib_number"], 101)
        self.assertEqual(result["outcome"], "debounced")
        self.assertIsNone(asyncio.run(self.engine.trigger_manual_crossing(999)))


class SimulatorEndpointTest(unittest.TestCase):
    """The simulator endpoints only work in mock mode."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:  # httpx missing (pip install -r requirements-dev.txt)
            raise unittest.SkipTest(f"fastapi TestClient unavailable: {e}")
        from web import app as web_app
        cls.web_app = web_app
        cls.tmp = tempfile.TemporaryDirectory()
        db = Database(os.path.join(cls.tmp.name, "manual_crossing_api.db"))
        cls.patches = [mock.patch.object(web_app, "db", db), mock.patch.object(web_app.lap_engine, "db", db)]
        for p in cls.patches:
            p.start()
        cls.client = TestClient(web_app.app)  # no lifespan: reader and flush loop stay off

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def setUp(self):
        if self.client.get("/api/event").json()["status"] == "running":
            self.client.post("/api/event/pause")
        res = self.client.post("/api/events", json={"name": f"Test {self._testMethodName}"})
        self.assertEqual(res.status_code, 200, res.text)
        res = self.client.post("/api/participants", json={"bib_number": 101, "name": "Alex", "epc": EPC_101})
        self.assertEqual(res.status_code, 200, res.text)

    def hardware(self, is_mock):
        """Put the already-imported app in mock or real-reader mode: the reader and the template flag."""
        from contextlib import ExitStack
        from hardware.mock_reader import MockReader
        reader = MockReader(on_tag_read=lambda r: None) if is_mock else mock.Mock(name="SerialReader")
        stack = ExitStack()
        stack.enter_context(mock.patch.object(self.web_app, "reader", reader))
        stack.enter_context(mock.patch.object(self.web_app, "use_mock", is_mock))
        return stack

    def laps(self):
        return self.web_app.db.get_laps_for_runner(101)

    def test_trigger_rejected_on_real_hardware(self):
        self.client.post("/api/event/start")
        with self.hardware(is_mock=False):
            res = self.client.post("/api/simulator/trigger/101")
            self.assertEqual(res.status_code, 400)
            self.assertIsNone(self.web_app.db.get_participant_by_bib(101)["start_time"])
            page = self.client.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('id="triggerCrossingBtn"', page.text)
        self.assertIn("raw reads logged", page.text)

    def test_trigger_in_mock_mode(self):
        self.client.post("/api/event/start")
        with self.hardware(is_mock=True):
            self.assertIn('id="triggerCrossingBtn"', self.client.get("/admin").text)
            first = self.client.post("/api/simulator/trigger/101")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["outcome"], "started")
            # Pressed again straight away: rejected, and no lap is made up
            again = self.client.post("/api/simulator/trigger/101")
            self.assertEqual(again.status_code, 409)
            self.assertEqual(self.laps(), [])
            self.assertEqual(self.client.post("/api/simulator/trigger/999").status_code, 404)

    def test_trigger_when_event_not_running(self):
        with self.hardware(is_mock=True):
            res = self.client.post("/api/simulator/trigger/101")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.laps(), [])


if __name__ == "__main__":
    unittest.main()
