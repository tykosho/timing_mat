"""The fake R200 (issue #67): same frames as the real module, the 65,535-poll stop, and faults."""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from core.db import Database
from core.lap_engine import LapEngine
from hardware.fake_r200 import (
    DEMO_EPCS, NO_TAG_RESPONSE, POLLS_PER_SECOND, STOP_RESPONSE, FakeR200, Faults,
    LappingRunners, replay_pty, serve_pty, tag_frame
)
from hardware.frame_capture import load_rx_bytes
from hardware.r200_protocol import (
    READ_MULTI_COMMAND, READ_STOP_COMMAND, build_command_frame, extract_frames, parse_inventory_response
)
from hardware.serial_reader import SerialReader

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "r200_capture_2026-11-15.log")
EPC = DEMO_EPCS[0]


class SimClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def frames_of(data: bytes):
    return extract_frames(bytearray(data))[0]


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class SameFramesAsTheRealModuleTest(unittest.TestCase):
    def test_tag_frames_are_byte_identical_to_the_capture(self):
        real = [f for f in frames_of(load_rx_bytes(FIXTURE)) if f[1] == 0x02]
        self.assertTrue(real)
        for frame in real:
            epc, rssi = parse_inventory_response(frame)
            self.assertEqual(tag_frame(epc, rssi), frame)

    def test_no_tag_response_is_the_captured_one(self):
        self.assertIn(NO_TAG_RESPONSE, frames_of(load_rx_bytes(FIXTURE)))

    def test_polls_at_the_measured_rate_once_armed(self):
        clock = SimClock()
        r200 = FakeR200(clock=clock)
        clock.t += 1.0
        self.assertEqual(r200.output(), b"", "silent until the host sends the multi-poll command")
        r200.receive(READ_MULTI_COMMAND)
        clock.t += 1.0
        frames = frames_of(r200.output())
        self.assertAlmostEqual(len(frames), POLLS_PER_SECOND, delta=1)
        self.assertEqual(set(frames), {NO_TAG_RESPONSE})

    def test_tag_on_the_antenna_is_read(self):
        clock = SimClock()
        r200 = FakeR200(clock=clock, seed=1)
        r200.receive(READ_MULTI_COMMAND)
        r200.place_tag(EPC, rssi=-41, seconds=1.0)
        clock.t += 2.0
        reads = [r for r in map(parse_inventory_response, frames_of(r200.output())) if r]
        self.assertGreater(len(reads), 10)
        self.assertEqual(set(reads), {(EPC, -41.0)})

    def test_stop_command_answers_and_stops(self):
        clock = SimClock()
        r200 = FakeR200(clock=clock)
        r200.receive(READ_MULTI_COMMAND)
        clock.t += 0.5
        r200.output()
        r200.receive(READ_STOP_COMMAND)
        self.assertEqual(r200.output(), STOP_RESPONSE)
        clock.t += 5.0
        self.assertEqual(r200.output(), b"")

    def test_commands_split_across_writes_and_garbled_ones(self):
        r200 = FakeR200(clock=SimClock())
        r200.receive(READ_MULTI_COMMAND[:4])
        self.assertFalse(r200.scanning)
        r200.receive(READ_MULTI_COMMAND[4:])
        self.assertTrue(r200.scanning)
        r200 = FakeR200(clock=SimClock())
        bad = bytearray(READ_MULTI_COMMAND)
        bad[-2] ^= 0xFF
        r200.receive(bytes(bad) + build_command_frame(0x03))  # bad checksum, then an unknown command
        self.assertFalse(r200.scanning)


class PollLimitTest(unittest.TestCase):
    """Issue #5: what we expect the real module to do with READ_MULTI_COMMAND's 0xFFFF."""

    def test_stops_after_65535_polls_about_31_minutes(self):
        clock = SimClock()
        r200 = FakeR200(clock=clock)
        r200.receive(READ_MULTI_COMMAND)
        stopped_after = None
        for second in range(1, 40 * 60):
            clock.t += 1.0
            r200.output()
            if not r200.scanning:
                stopped_after = second
                break
        self.assertEqual(r200.polls, 0xFFFF)
        self.assertEqual(r200.stops_at_limit, 1)
        self.assertAlmostEqual(stopped_after / 60, 0xFFFF / POLLS_PER_SECOND / 60, delta=0.1)  # ~31.2 min

    def test_rearming_starts_a_new_count(self):
        clock = SimClock()
        r200 = FakeR200(clock=clock, poll_limit=35)
        r200.receive(READ_MULTI_COMMAND)
        clock.t += 2.0
        self.assertEqual(len(frames_of(r200.output())), 35)
        clock.t += 2.0
        self.assertEqual(r200.output(), b"")
        r200.receive(READ_STOP_COMMAND + READ_MULTI_COMMAND)  # what SerialReader's re-arm sends
        clock.t += 0.5
        self.assertGreater(len(frames_of(r200.output())), 10)


class SerialReaderAgainstTheFakeTest(unittest.TestCase):
    """The real SerialReader thread, talking to the fake through serial_factory."""

    def start(self, r200, **kw):
        reads = []
        reader = SerialReader(on_tag_read=reads.append, serial_factory=r200.serial_factory, **kw)
        reader.start()
        self.addCleanup(reader.stop)
        return reader, reads

    def test_watchdog_brings_back_a_reader_that_hit_the_poll_limit(self):
        """Issue #5's failure, reproduced: without the re-arm the reads stop for good."""
        for watchdog in (False, True):
            with self.subTest(watchdog=watchdog):
                r200 = FakeR200(poll_limit=20, seed=2)  # stops after ~0.6 s instead of ~31 min
                r200.place_tag(EPC, seconds=60)
                reader, reads = self.start(r200, rearm_interval=0, silence_rearm=0.5 if watchdog else 0)
                self.assertTrue(wait_for(lambda: r200.stops_at_limit >= 1))
                stopped_at = len(reads)
                self.assertGreater(stopped_at, 0)
                if watchdog:
                    self.assertTrue(wait_for(lambda: len(reads) > stopped_at), "re-armed and reading again")
                    self.assertGreaterEqual(reader.rearm_count, 1)
                else:
                    time.sleep(1.2)
                    self.assertEqual(len(reads), stopped_at, "no watchdog: the module stays stopped")

    def test_unplug_and_replug(self):
        r200 = FakeR200(seed=3)
        r200.place_tag(EPC, seconds=60)
        reader, reads = self.start(r200)
        self.assertTrue(wait_for(lambda: len(reads) > 5))
        r200.unplug()
        self.assertTrue(wait_for(lambda: not reader.health()["connected"]))
        self.assertTrue(wait_for(lambda: "no device" in (reader.health()["last_error"] or "")),
                        reader.health()["last_error"])
        before = len(reads)
        r200.replug()  # the module lost power: it only scans again once SerialReader re-arms it
        self.assertTrue(wait_for(lambda: reader.health()["connected"] and len(reads) > before))
        self.assertEqual(reader.health()["reconnects"], 1)

    def test_a_hung_module_goes_quiet_and_rearming_does_not_help(self):
        r200 = FakeR200(seed=4)
        r200.place_tag(EPC, seconds=60)
        reader, reads = self.start(r200, rearm_interval=0, silence_rearm=0.3)
        self.assertTrue(wait_for(lambda: len(reads) > 5))
        r200.hang()
        time.sleep(0.1)
        count = len(reads)
        self.assertTrue(wait_for(lambda: reader.rearm_count >= 2))
        self.assertEqual(len(reads), count)
        self.assertGreater(reader.health()["seconds_since_frame"], 0.25)  # amber on the admin page
        r200.power_cycle()
        self.assertTrue(wait_for(lambda: len(reads) > count), "the next silence re-arm wakes it")


class HungReaderShowsAmberTest(unittest.TestCase):
    """Regression (found with the fake): each silence re-arm used to reset "last frame", so with
    the defaults (re-arm after 30 s, amber after 30 s) a hung module stayed green for ever."""

    def test_defaults(self):
        from web.reader_health import ReaderHealth
        clock = SimClock()
        r200 = FakeR200(clock=clock, seed=6)
        r200.place_tag(EPC, seconds=600)
        reader = SerialReader(serial_factory=r200.serial_factory, clock=clock)
        health = ReaderHealth(clock=clock)
        reader.serial_conn = r200.serial_factory(timeout=0)
        reader.connected = True
        reader._connected_at = clock.t
        reader._arm()

        buffer = bytearray()
        states = []
        for second in range(120):
            if second == 10:
                r200.hang()
            clock.t += 1.0
            buffer.extend(reader.serial_conn.read(1 << 20))
            buffer = reader._process_stream(buffer)
            reader._maybe_rearm()
            states.append(health.snapshot(reader.health())["state"])

        self.assertEqual(set(states[:10]), {"ok"})
        self.assertGreaterEqual(reader.rearm_count, 3, "the watchdog keeps trying")
        self.assertEqual(states[-1], "quiet")
        self.assertEqual(reader.health()["seconds_since_frame"], 110.0)


class FaultsTest(unittest.TestCase):
    def run_reader(self, faults, seconds=30.0, seed=5):
        clock = SimClock()
        r200 = FakeR200(clock=clock, faults=faults, seed=seed)
        reads = []
        reader = SerialReader(on_tag_read=reads.append)
        r200.receive(READ_MULTI_COMMAND)
        r200.place_tag(EPC, rssi=-45, seconds=seconds)
        buffer = bytearray()
        for _ in range(int(seconds * 10)):
            clock.t += 0.1
            buffer.extend(r200.output())
            buffer = reader._process_stream(buffer)
        return r200, reads

    def test_corruption_and_noise_never_invent_a_tag(self):
        r200, reads = self.run_reader(Faults(corrupt=0.2, noise=0.1, drop=0.05))
        self.assertGreater(r200.corrupted, 50)
        self.assertGreater(r200.dropped, 10)
        self.assertGreater(len(reads), 200, "most reads still get through")
        self.assertEqual({r.epc for r in reads}, {EPC})
        self.assertEqual({r.rssi for r in reads}, {-45.0})

    def test_rssi_jitter(self):
        _, reads = self.run_reader(Faults(rssi_jitter=4.0), seconds=5)
        rssis = {r.rssi for r in reads}
        self.assertGreater(len(rssis), 5)
        self.assertTrue(all(-70 < r < -20 for r in rssis))


class FullRaceThroughTheSerialPathTest(unittest.TestCase):
    """Runners lapping the fake reader, through SerialReader and the lap engine, with faults."""

    def test_every_lap_is_counted_despite_faults(self):
        clock = SimClock(1000.0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(os.path.join(tmp.name, "race.db"))
        db.seed_demo_runners()
        db.update_event_settings(min_lap_seconds=45.0)
        db.start_event(at=clock.t)

        r200 = FakeR200(clock=clock, seed=7, faults=Faults(corrupt=0.05, drop=0.05, noise=0.02, rssi_jitter=3.0))
        runners = LappingRunners(r200, DEMO_EPCS, lap_seconds=75.0, seed=7)
        crossings = {epc: 0 for epc in DEMO_EPCS}
        place = r200.place_tag

        def counting_place(epc, **kw):
            crossings[epc] += 1
            place(epc, **kw)
        r200.place_tag = counting_place

        unknown = []
        engine = LapEngine(db=db, crossing_window=2.5, on_unassigned_tag=unknown.append)
        reader = SerialReader(on_tag_read=engine.process_tag_read, clock=clock)

        async def race():
            buffer = bytearray()
            r200.receive(READ_MULTI_COMMAND)
            while clock.t < 1000.0 + 20 * 60:
                clock.t += 0.2
                runners.tick(clock.t)
                buffer.extend(r200.output())
                buffer = reader._process_stream(buffer)
                await engine.flush_once(clock.t)
            clock.t += 10.0
            await engine.flush_once(clock.t)

        with mock.patch("hardware.serial_reader.clock.now", clock):
            asyncio.run(race())

        self.assertEqual(unknown, [])
        self.assertGreater(r200.corrupted, 0)
        for bib, epc in enumerate(DEMO_EPCS, start=101):
            with self.subTest(bib=bib):
                laps = db.get_laps_for_runner(bib)
                self.assertGreaterEqual(crossings[epc], 15)
                self.assertEqual(len(laps), crossings[epc] - 1, "first crossing starts the runner")


@unittest.skipUnless(os.name == "posix", "needs a pty")
class VirtualSerialPortTest(unittest.TestCase):
    """The pty the CLI serves: pyserial and SerialReader open it like the real /dev/ttyUSB0."""

    def serve(self, target, *args, **kw):
        ports = []
        stop = threading.Event()
        if target is serve_pty:
            kw["stop"] = stop
        kw["announce"] = lambda s: ports.append(s) if s.startswith("/") else None
        thread = threading.Thread(target=target, args=args, kwargs=kw, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(stop.set)
        self.assertTrue(wait_for(lambda: ports))
        return ports[0]

    def test_serial_reader_on_the_pty(self):
        r200 = FakeR200(seed=8)
        r200.place_tag(EPC, rssi=-44, seconds=60)
        port = self.serve(serve_pty, r200)
        reads = []
        reader = SerialReader(port=port, on_tag_read=reads.append)
        reader.start()
        self.addCleanup(reader.stop)
        self.assertTrue(wait_for(lambda: len(reads) > 5), reader.health())
        self.assertEqual({(r.epc, r.rssi) for r in reads}, {(EPC, -44.0)})

    def test_replaying_the_real_capture(self):
        port = self.serve(replay_pty, FIXTURE, speed=40.0)
        reads = []
        reader = SerialReader(port=port, on_tag_read=reads.append)
        reader.start()
        self.addCleanup(reader.stop)
        expected = [r for r in map(parse_inventory_response, frames_of(load_rx_bytes(FIXTURE))) if r]
        self.assertTrue(wait_for(lambda: len(reads) >= len(expected), timeout=10), len(reads))
        self.assertEqual([(r.epc, r.rssi) for r in reads], expected)


if __name__ == "__main__":
    unittest.main()
