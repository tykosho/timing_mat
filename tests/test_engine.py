import os
import time
import asyncio
import unittest
from core.db import Database
from core.models import TagRead
from core.lap_engine import LapEngine
from hardware.r200_protocol import (
    build_command_frame,
    parse_inventory_response,
    CMD_SINGLE_POLLING,
    HEADER,
    FOOTER,
)

class TestTimingMat(unittest.TestCase):
    def setUp(self):
        self.db_path = "data/test_race.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = Database(self.db_path)
        self.db.seed_demo_runners()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_database_and_participants(self):
        participants = self.db.get_all_participants()
        self.assertGreaterEqual(len(participants), 5)
        p101 = self.db.get_participant_by_bib(101)
        self.assertIsNotNone(p101)
        self.assertEqual(p101["name"], "Alex Chen")

    def test_r200_protocol_framing(self):
        cmd = build_command_frame(CMD_SINGLE_POLLING, payload=b"\x00\x00")
        self.assertEqual(cmd[0], HEADER)
        self.assertEqual(cmd[-1], FOOTER)

        # Construct a synthetic R200 inventory response frame:
        epc_bytes = bytes.fromhex("E28011700000020101010001")
        payload = bytes([0x45, 0x30, 0x00]) + epc_bytes
        frame = bytes([HEADER, 0x02, CMD_SINGLE_POLLING, 0x00, len(payload)]) + payload + bytes([0x00, FOOTER])
        
        parsed = parse_inventory_response(frame)
        self.assertIsNotNone(parsed)
        epc, rssi = parsed
        self.assertEqual(epc, "E28011700000020101010001")

    def test_chip_start_and_lap_detection(self):
        laps_recorded = []
        started_runners = []
        resumed_runners = []

        # Event started at t=1000
        t0 = 1000.0
        self.db.update_event_state(is_active=True, start_time=t0, min_lap_seconds=45.0, max_lap_rest_seconds=300.0)

        engine = LapEngine(
            db=self.db,
            crossing_window=0.5,
            on_lap_completed=lambda l: laps_recorded.append(l),
            on_runner_started=lambda s: started_runners.append(s),
            on_runner_resumed=lambda r: resumed_runners.append(r)
        )

        epc = "E28011700000020101010001" # Bib 101

        # 1. First crossing (t = 1100s, 100s after event gun) -> Should register CHIP START (Lap 0)
        asyncio.run(engine._resolve_crossing(epc, [(1100.0, -45.0)]))
        self.assertEqual(len(started_runners), 1)
        self.assertEqual(started_runners[0]["bib_number"], 101)
        self.assertEqual(len(laps_recorded), 0, "First crossing must be start line, not lap 1")

        p = self.db.get_participant_by_bib(101)
        self.assertEqual(p["start_time"], 1100.0)

        # 2. Debounce crossing (t = 1115s, only 15s later) -> Rejected by 45s debounce
        asyncio.run(engine._resolve_crossing(epc, [(1115.0, -44.0)]))
        self.assertEqual(len(laps_recorded), 0)

        # 3. Second valid crossing (t = 1170s, 70s later) -> Should complete LAP 1
        asyncio.run(engine._resolve_crossing(epc, [(1170.0, -42.0)]))
        self.assertEqual(len(laps_recorded), 1)
        self.assertEqual(laps_recorded[0]["lap_number"], 1)
        self.assertEqual(laps_recorded[0]["lap_duration_seconds"], 70.0)

        # 4. Long break (t = 1600s, 430s later > 300s rest threshold) -> Should detect REST BREAK & RESUME
        asyncio.run(engine._resolve_crossing(epc, [(1600.0, -43.0)]))
        self.assertEqual(len(resumed_runners), 1)
        self.assertEqual(len(laps_recorded), 1, "Rest resume crossing should not record slow lap")
        breaks = self.db.get_breaks_for_runner(101)
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0]["duration_seconds"], 430.0)

        # 5. Subsequent crossing (t = 1675s, 75s after resuming) -> Should complete LAP 2 cleanly!
        asyncio.run(engine._resolve_crossing(epc, [(1675.0, -41.0)]))
        self.assertEqual(len(laps_recorded), 2)
        self.assertEqual(laps_recorded[1]["lap_number"], 2)
        self.assertEqual(laps_recorded[1]["lap_duration_seconds"], 75.0)

    def test_mock_reader_burst(self):
        reads = []
        from hardware.mock_reader import MockReader
        mock = MockReader(on_tag_read=lambda r: reads.append(r), speed_multiplier=10.0)
        
        mock._simulate_crossing_burst("E28011700000020101010001")
        self.assertGreaterEqual(len(reads), 5)
        for r in reads:
            self.assertEqual(r.epc, "E28011700000020101010001")
            self.assertTrue(-85.0 <= r.rssi <= -35.0)

if __name__ == "__main__":
    unittest.main()
