import os
import time
import asyncio
import unittest
from core.db import Database
from core.lap_engine import LapEngine
from hardware.r200_protocol import (
    build_command_frame, calculate_checksum, extract_frames, parse_inventory_response, parse_tag_frame,
    CMD_MULTI_POLLING, CMD_STOP_MULTI, READ_MULTI_COMMAND, READ_STOP_COMMAND
)

def make_tag_frame(epc_hex: str, raw_rssi: int = 0xC9, pc_msb: int = 0x30) -> bytes:
    """Builds an R200 tag notification: AA 02 22 LEN RSSI PC(2) EPC CRC(2) CS DD."""
    payload = bytes([raw_rssi, pc_msb, 0x00]) + bytes.fromhex(epc_hex) + b"\x12\x34"
    body = bytes([0x02, 0x22, len(payload) >> 8, len(payload) & 0xFF]) + payload
    return bytes([0xAA]) + body + bytes([calculate_checksum(body), 0xDD])

class TestR200Protocol(unittest.TestCase):
    EPC = "E28011700000020101010001"

    def test_command_frames(self):
        self.assertEqual(build_command_frame(CMD_MULTI_POLLING, b"\x22\xff\xff"), READ_MULTI_COMMAND)
        self.assertEqual(build_command_frame(CMD_STOP_MULTI), READ_STOP_COMMAND)

    def test_parse_tag_frame(self):
        self.assertEqual(parse_tag_frame(make_tag_frame(self.EPC, raw_rssi=0xC9)), (self.EPC, 0xC9))
        epc, dbm = parse_inventory_response(make_tag_frame(self.EPC, raw_rssi=0xC9))
        self.assertEqual(epc, self.EPC)
        self.assertLess(dbm, 0)  # 0xC9 = -55 dBm per R200 user protocol V2.3.3 p.42

    def test_rssi_is_not_the_pc_byte(self):
        """Regression: the old sniffer reported the PC high byte (0x34 = 52) as RSSI."""
        _, raw = parse_tag_frame(make_tag_frame(self.EPC, raw_rssi=0x2A, pc_msb=0x34))
        self.assertEqual(raw, 0x2A)

    def test_non_tag_frames_ignored(self):
        self.assertIsNone(parse_tag_frame(READ_MULTI_COMMAND))
        response = bytes([0xAA, 0x01, 0x27, 0x00, 0x01, 0x00, 0x29, 0xDD])
        self.assertIsNone(parse_tag_frame(response))

    def test_frames_emitted_per_chunk_not_after_buffer_fills(self):
        """Regression: each tag frame must be available as soon as its bytes arrive."""
        frame = make_tag_frame(self.EPC)
        buf = bytearray()
        got = []
        for i in range(0, len(frame), 5):  # dribble the frame in 5-byte chunks
            buf.extend(frame[i:i + 5])
            frames, buf = extract_frames(buf)
            got.extend(frames)
        self.assertEqual(got, [frame])
        self.assertEqual(len(buf), 0)

    def test_extract_frames_resyncs_after_noise(self):
        f1 = make_tag_frame(self.EPC)
        f2 = make_tag_frame("E28011700000020101010002")
        buf = bytearray(b"\x00\xAA\x13" + f1 + b"\xFF\xFF" + f2)
        frames, rest = extract_frames(buf)
        self.assertEqual(frames, [f1, f2])
        self.assertEqual(len(rest), 0)

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

    def test_chip_start_and_lap_detection(self):
        laps_recorded = []
        started_runners = []
        resumed_runners = []

        # Event started at t=1000
        t0 = 1000.0
        self.db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=300.0)
        self.db.start_event(at=t0)

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

    def test_mock_reader_scaled_time_and_pause(self):
        from hardware.mock_reader import MockReader
        reads = []
        mock = MockReader(on_tag_read=lambda r: reads.append(r), speed_multiplier=25.0)
        mock.start()
        try:
            import time
            time.sleep(0.25)
            state1 = mock.get_state()
            self.assertGreater(state1["sim_time"], 2.0)

            mock.set_paused(True)
            paused_sim_time = mock.get_state()["sim_time"]
            time.sleep(0.2)
            self.assertAlmostEqual(mock.get_state()["sim_time"], paused_sim_time, places=2)
        finally:
            mock.stop()

    def test_gpx_sample_lap_extraction(self):
        from hardware.mock_reader import load_gpx_lap_durations, DEFAULT_SAMPLE_GPX
        laps = load_gpx_lap_durations(DEFAULT_SAMPLE_GPX)
        self.assertGreaterEqual(len(laps), 50)
        self.assertTrue(all(dur > 60.0 for dur in laps))

    def test_unassigned_tag_callback(self):
        import tempfile
        unassigned = []
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, "test.db"))
            engine = LapEngine(
                db=db,
                crossing_window=0.2,
                on_unassigned_tag=lambda data: unassigned.append(data),
            )
            now = time.time()
            asyncio.run(engine._resolve_crossing("E28099990000000000000099", [(now, -52.0), (now + 0.05, -48.0)]))
            self.assertEqual(len(unassigned), 1)
            self.assertEqual(unassigned[0]["epc"], "E28099990000000000000099")
            self.assertEqual(unassigned[0]["peak_rssi"], -48.0)

            # Once registered, _resolve_crossing should NOT fire on_unassigned_tag
            db.add_participant(7, "Walk-Up Runner", "E28099990000000000000099")
            asyncio.run(engine._resolve_crossing("E28099990000000000000099", [(now + 1.0, -50.0)]))
            self.assertEqual(len(unassigned), 1)

if __name__ == "__main__":
    unittest.main()
