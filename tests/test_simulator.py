import asyncio
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from core.db import Database
from core.lap_engine import LapEngine
from core.models import TagRead
from hardware.mock_reader import MockReader


class TestMessageSimulatorEngine(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.db_path)
        self.db.seed_demo_runners()
        self.db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=300.0)
        self.db.start_event(at=1000.0)

        self.laps = []
        self.starts = []
        self.engine = LapEngine(
            db=self.db,
            crossing_window=2.5,
            on_lap_completed=lambda d: self.laps.append(d),
            on_runner_started=lambda d: self.starts.append(d),
        )
        self.engine.refresh_event()

        # Automatically flush a burst whenever a gap >= crossing_window occurs for an EPC
        self._last_ts = {}
        self._pending_bursts = []

        def on_read(read: TagRead):
            prev = self._last_ts.get(read.epc)
            if prev is not None and (read.timestamp - prev) >= self.engine.crossing_window:
                reads = self.engine.buffers.pop(read.epc, [])
                if reads:
                    self._pending_bursts.append((read.epc, reads))
            self._last_ts[read.epc] = read.timestamp
            self.engine.process_tag_read(read)

        self.sim = MockReader(on_tag_read=on_read, speed_multiplier=1.0)
        self.sim.participant_provider = self.db.get_all_participants
        self.sim.event_state_provider = self.db.get_event_state

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _flush_all(self):
        async def _run():
            for epc, reads in self._pending_bursts:
                await self.engine._resolve_crossing(epc, reads)
            self._pending_bursts.clear()
            for epc in list(self.engine.buffers.keys()):
                reads = self.engine.buffers.pop(epc, [])
                if reads:
                    await self.engine._resolve_crossing(epc, reads)
            self._last_ts.clear()

        asyncio.run(_run())

    def test_happy_path_and_double_bounce_debounced(self):
        # 1. Start #101 at t=1010
        self.sim.run_scenario("normal", bib=101, blocking=False, base_time=1010.0)
        self._flush_all()
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(len(self.laps), 0)

        # 2. Double-bounce at t=1075: crosses for Lap 1, then bounces 3.7s later -> only 1 lap recorded!
        ev = self.sim.run_scenario("double_bounce", bib=101, blocking=False, base_time=1075.0)
        self._flush_all()
        self.assertTrue(ev["is_error"])
        self.assertEqual(len(self.laps), 1)
        self.assertEqual(self.laps[0]["lap_number"], 1)
        self.assertAlmostEqual(self.laps[0]["lap_duration_seconds"], 65.0, delta=1.5)

    def test_missed_read_causes_double_duration_next_lap(self):
        # Start #101 at t=1010
        self.sim.run_scenario("normal", bib=101, blocking=False, base_time=1010.0)
        self._flush_all()

        # Missed read at t=1070 (0 messages sent)
        ev = self.sim.run_scenario("missed_read", bib=101, blocking=False, base_time=1070.0)
        self._flush_all()
        self.assertEqual(ev["reads_emitted"], 0)
        self.assertEqual(len(self.laps), 0)

        # Next crossing at t=1130 records a ~120s lap
        self.sim.run_scenario("normal", bib=101, blocking=False, base_time=1130.0)
        self._flush_all()
        self.assertEqual(len(self.laps), 1)
        self.assertAlmostEqual(self.laps[0]["lap_duration_seconds"], 120.0, delta=1.5)

    def test_double_count_records_two_laps(self):
        self.sim.run_scenario("normal", bib=101, blocking=False, base_time=1010.0)
        self._flush_all()

        # Double count at t=1070 emits two bursts separated by effective_min_lap + 1.5s
        self.sim.run_scenario("double_count", bib=101, blocking=False, base_time=1070.0)
        self._flush_all()
        self.assertEqual(len(self.laps), 2)
        self.assertEqual(self.laps[0]["lap_number"], 1)
        self.assertEqual(self.laps[1]["lap_number"], 2)
        self.assertAlmostEqual(self.laps[1]["lap_duration_seconds"], 46.5, delta=1.5)

    def test_mass_start_and_unknown_tag(self):
        ev = self.sim.run_scenario("mass_start", blocking=False, base_time=1005.0)
        self._flush_all()
        self.assertEqual(len(self.starts), 5)
        self.assertEqual(len(ev["bibs"]), 5)

        unk = self.sim.run_scenario("unknown_tag", blocking=False, base_time=1020.0)
        self._flush_all()
        self.assertGreater(unk["reads_emitted"], 0)
        self.assertEqual(len(self.laps), 0)

    def test_presets_and_status(self):
        self.sim.set_preset("glitchy")
        self.assertEqual(self.sim.preset, "glitchy")
        self.assertTrue(self.sim.auto_crossing_enabled)
        self.assertGreater(self.sim.weights["double_bounce"], 0.0)

        self.sim.set_preset("manual")
        self.assertFalse(self.sim.auto_crossing_enabled)
        status = self.sim.get_status()
        self.assertEqual(status["preset"], "manual")
        self.assertIn("scenarios", status)


class TestSimulatorHTTPAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import app as app_module
        cls.app_module = app_module
        cls._tmpdir = tempfile.TemporaryDirectory()
        db = Database(os.path.join(cls._tmpdir.name, "sim_api.db"))
        db.seed_demo_runners()
        cls.patches = [
            mock.patch.object(app_module, "db", db),
            mock.patch.object(app_module.lap_engine, "db", db),
        ]
        for p in cls.patches:
            p.start()
        cls.client = TestClient(app_module.app)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls._tmpdir.cleanup()

    def test_simulator_page_and_api(self):
        with mock.patch.object(
            self.app_module,
            "reader",
            MockReader(on_tag_read=self.app_module.lap_engine.process_tag_read),
        ):
            page = self.client.get("/simulator")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Hardware Message &amp; Glitch Simulator", page.text)

            status = self.client.get("/api/simulator/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["preset"], "happy_path")

            preset_res = self.client.post("/api/simulator/preset", json={"preset": "realistic"})
            self.assertEqual(preset_res.status_code, 200)
            self.assertEqual(preset_res.json()["preset"], "realistic")

            scen_res = self.client.post(
                "/api/simulator/scenario",
                json={"scenario": "missed_read", "bib": 101},
            )
            self.assertEqual(scen_res.status_code, 200)
            self.assertEqual(scen_res.json()["scenario"], "missed_read")

            clear_res = self.client.post("/api/simulator/clear-log")
            self.assertEqual(clear_res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
