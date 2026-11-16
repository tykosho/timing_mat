"""
A fake R200 on the serial line (issue #67, Tasks "Simulator level 1").

MockReader skips the serial code entirely: it hands TagReads straight to the app. This module
pretends to be the module itself and speaks raw frames, so SerialReader's whole path (frame
splitting, checksums, the watchdog re-arm, reconnects) runs exactly as it does on the Pi.

What it copies from the real reader, measured in tests/fixtures/r200_capture_2026-11-15.log:
  - it starts polling on the multi-poll command (AA 00 27 00 03 22 <count> CS DD) and stops on
    AA 00 28 ...; about 35 polls per second;
  - each poll answers with a tag notification (AA 02 22 ...) per tag read, or with the
    "no tag" response AA 01 FF 00 01 15 16 DD;
  - tag frames carry RSSI (signed dBm), PC 0x3000 for a 96-bit EPC and the tag's CRC-16.
And what we believe it does but haven't confirmed (issue #5): it stops after <count> polls,
which is 65,535 in our command, so about 31 minutes at 35 polls/s.

Faults to switch on: corrupted bytes, dropped frames, line noise, RSSI jitter, a hang that
only a power cycle cures, and unplugging the USB cable.

Three ways to use it:
  - in tests: SerialReader(serial_factory=FakeR200(...).serial_factory)
  - as a virtual serial port for the real app or the sniffer:
        python -m hardware.fake_r200 --runners 8
        MOCK_HARDWARE=false SERIAL_PORT=<printed path> python -m uvicorn web.app:app
  - to replay a recorded capture through that port: python -m hardware.fake_r200 --replay FILE
"""
import argparse
import os
import random
import select
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from hardware.r200_protocol import (
    CMD_MULTI_POLLING, CMD_STOP_MULTI, FOOTER, HEADER, calculate_checksum, extract_frames
)

POLLS_PER_SECOND = 35.0       # measured on the real module (2,827 responses in 80.9 s)
NO_TAG_RESPONSE = bytes.fromhex("AA01FF00011516DD")
STOP_RESPONSE = bytes.fromhex("AA01280001002ADD")  # type 01, cmd 28, payload 00 = OK
MAX_CATCH_UP_POLLS = 35 * 60  # after a long pause, simulate at most a minute of polls
DEMO_EPCS = [f"E2801170000002010101000{i}" for i in range(1, 6)]  # db.seed_demo_runners()


def epc_crc16(data: bytes) -> int:
    """EPC Gen2 CRC-16 over PC + EPC, as the tag sends it (matches every captured frame)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc ^ 0xFFFF


def tag_frame(epc_hex: str, rssi_dbm: float) -> bytes:
    """A tag notification frame exactly as the R200 sends it."""
    epc = bytes.fromhex(epc_hex)
    if len(epc) % 2:
        raise ValueError("EPC must be a whole number of 16-bit words")
    pc = bytes([(len(epc) // 2) << 3, 0x00])
    raw_rssi = max(-128, min(127, int(round(rssi_dbm)))) & 0xFF
    payload = bytes([raw_rssi]) + pc + epc + epc_crc16(pc + epc).to_bytes(2, "big")
    body = bytes([0x02, 0x22, len(payload) >> 8, len(payload) & 0xFF]) + payload
    return bytes([HEADER]) + body + bytes([calculate_checksum(body), FOOTER])


@dataclass
class Faults:
    corrupt: float = 0.0      # chance that a frame has one byte changed on the wire
    drop: float = 0.0         # chance that a frame never arrives
    noise: float = 0.0        # chance of junk bytes (with stray 0xAA) before a frame
    rssi_jitter: float = 0.0  # standard deviation, dB, added to each tag read


class FakeR200:
    """The module's behaviour, driven by a clock you can control. Thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 polls_per_second: float = POLLS_PER_SECOND,
                 poll_limit: Optional[int] = None,
                 read_chance: float = 0.6,
                 max_tags_per_poll: Optional[int] = None,
                 faults: Optional[Faults] = None,
                 seed: Optional[int] = None):
        """poll_limit overrides the count in the host's command (to reach the stop quickly)."""
        self.clock = clock
        self.polls_per_second = polls_per_second
        self.poll_limit = poll_limit
        self.read_chance = read_chance
        self.max_tags_per_poll = max_tags_per_poll  # a crowd on the antenna: not every tag answers
        self.faults = faults or Faults()
        self.rng = random.Random(seed)
        self._lock = threading.Lock()
        self._rx = bytearray()          # bytes from the host, not yet a whole frame
        self._out = bytearray()         # bytes waiting to go to the host
        self._tags: Dict[str, List[Tuple[float, float, float]]] = {}  # epc -> [(from, until, rssi)]
        self.scanning = False
        self.polls_left = 0
        self._next_poll_at = 0.0
        self.hung = False
        self.unplugged = False
        self._port: Optional["FakeSerialPort"] = None
        # Counters, for tests and the CLI
        self.polls = 0
        self.tag_frames = 0
        self.arms = 0
        self.stops_at_limit = 0
        self.corrupted = 0
        self.dropped = 0

    # --- the world: tags on the antenna ---
    def place_tag(self, epc: str, rssi: float = -45.0, seconds: float = 1.0, at: Optional[float] = None):
        """Puts a tag in the field from `at` (default now) for `seconds`, at peak `rssi` dBm."""
        start = self.clock() if at is None else at
        with self._lock:
            self._tags.setdefault(epc.upper(), []).append((start, start + seconds, rssi))

    def _tags_at(self, t: float) -> List[Tuple[str, float]]:
        present = []
        for epc, spans in list(self._tags.items()):
            spans[:] = [s for s in spans if s[1] >= t - 5.0]  # forget old passes
            for start, until, rssi in spans:
                if start <= t <= until:
                    present.append((epc, rssi))
                    break
            if not spans:
                del self._tags[epc]
        return present

    # --- host -> module ---
    def receive(self, data: bytes):
        with self._lock:
            if self.hung:
                return
            self._rx.extend(data)
            frames, self._rx = extract_frames(self._rx)
            for frame in frames:
                self._command(frame)

    def _command(self, frame: bytes):
        if frame[1] != 0x00 or calculate_checksum(frame[1:-2]) != frame[-2]:
            return  # not a command, or garbled: the module ignores it
        cmd, payload = frame[2], frame[5:-2]
        if cmd == CMD_MULTI_POLLING and len(payload) == 3:
            count = (payload[1] << 8) | payload[2]
            self.polls_left = count if self.poll_limit is None else min(count, self.poll_limit)
            if not self.scanning:
                self._next_poll_at = self.clock()
            self.scanning = True
            self.arms += 1
        elif cmd == CMD_STOP_MULTI:
            self.scanning = False
            self._out.extend(STOP_RESPONSE)

    # --- module -> host ---
    def output(self) -> bytes:
        """Everything the module has sent since the last call."""
        with self._lock:
            if not self.hung:
                self._run_polls(self.clock())
            data, self._out = bytes(self._out), bytearray()
            return data

    def _run_polls(self, now: float):
        if not self.scanning:
            return
        if now < self._next_poll_at:
            return
        due = int((now - self._next_poll_at) * self.polls_per_second) + 1
        if due > MAX_CATCH_UP_POLLS:
            self._next_poll_at = now - MAX_CATCH_UP_POLLS / self.polls_per_second
            due = MAX_CATCH_UP_POLLS
        for _ in range(due):
            if self.polls_left <= 0:
                self.scanning = False  # the 65,535-poll stop (issue #5)
                self.stops_at_limit += 1
                return
            t = self._next_poll_at
            self._next_poll_at += 1.0 / self.polls_per_second
            self.polls_left -= 1
            self.polls += 1
            frames = []
            present = self._tags_at(t)
            if self.max_tags_per_poll and len(present) > self.max_tags_per_poll:
                present = self.rng.sample(present, self.max_tags_per_poll)
            for epc, rssi in present:
                if self.rng.random() < self.read_chance:
                    if self.faults.rssi_jitter:
                        rssi += self.rng.gauss(0.0, self.faults.rssi_jitter)
                    frames.append(tag_frame(epc, rssi))
                    self.tag_frames += 1
            for frame in frames or [NO_TAG_RESPONSE]:
                self._send(frame)

    def _send(self, frame: bytes):
        f, rng = self.faults, self.rng
        if f.noise and rng.random() < f.noise:
            self._out.extend(bytes([HEADER, rng.randrange(256), 0x00, rng.randrange(256)]))
        if f.drop and rng.random() < f.drop:
            self.dropped += 1
            return
        if f.corrupt and rng.random() < f.corrupt:
            frame = bytearray(frame)
            i = rng.randrange(1, len(frame) - 1)  # header and footer intact: only the checksum can tell
            frame[i] ^= 1 << rng.randrange(8)
            frame = bytes(frame)
            self.corrupted += 1
        self._out.extend(frame)

    # --- failures you can trigger ---
    def hang(self):
        """Firmware lock-up: ignores commands and sends nothing until power_cycle()."""
        with self._lock:
            self.hung = True

    def power_cycle(self):
        with self._lock:
            self.hung = False
            self.scanning = False
            self._rx.clear()
            self._out.clear()

    def unplug(self):
        """Pulls the USB cable: the open port fails, and opening it fails until replug()."""
        self.unplugged = True
        if self._port:
            self._port.is_open = False

    def replug(self):
        self.power_cycle()  # the module loses power with the cable
        self.unplugged = False

    def serial_factory(self, port: str = "/dev/ttyUSB0", **kwargs) -> "FakeSerialPort":
        """Drop-in for serial.Serial in SerialReader(serial_factory=...)."""
        if self.unplugged:
            raise OSError(f"[Errno 2] could not open port {port}: No such file or directory: '{port}'")
        self._port = FakeSerialPort(self, timeout=kwargs.get("timeout", 0.1))
        return self._port


class FakeSerialPort:
    """The pyserial subset SerialReader uses, connected to a FakeR200."""

    def __init__(self, device: FakeR200, timeout: float = 0.1):
        self.device = device
        self.timeout = timeout
        self.is_open = True
        self._pending = bytearray()

    def _check(self):
        if not self.is_open or self.device.unplugged:
            raise OSError("[Errno 6] Device not configured")

    def write(self, data: bytes) -> int:
        self._check()
        self.device.receive(bytes(data))
        return len(data)

    def flush(self):
        self._check()

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        while True:
            self._check()
            self._pending.extend(self.device.output())
            if self._pending or time.monotonic() >= deadline:
                break
            time.sleep(0.005)
        data, self._pending = bytes(self._pending[:size]), self._pending[size:]
        return data

    def close(self):
        self.is_open = False


class LappingRunners:
    """Runners going round the track, crossing the mat every lap.

    With pack_size > 1, runners are grouped into packs that keep the same pace for the whole
    race and cross the mat within `pack_gap` seconds of each other, the hardest case for the
    reader. `crossings` counts each runner's crossings, the truth to check laps against.
    """

    def __init__(self, device: FakeR200, epcs: List[str], lap_seconds: float = 75.0,
                 spread: float = 0.2, on_mat_seconds: float = 1.2, seed: Optional[int] = None,
                 pack_size: int = 1, pack_gap: float = 1.5, start_spread: float = 4.0):
        self.device = device
        self.epcs = epcs
        self.on_mat_seconds = on_mat_seconds
        self.pack_gap = pack_gap
        self.rng = random.Random(seed)
        self.crossings: Dict[str, int] = {epc: 0 for epc in epcs}
        start = device.clock()
        size = max(1, pack_size)
        # Everyone crosses at the start within a few seconds (chip start), then laps
        self.packs = []
        for i in range(0, len(epcs), size):
            self.packs.append({
                "epcs": epcs[i:i + size],
                "next_at": start + self.rng.uniform(0, start_spread),
                "pace": lap_seconds * self.rng.uniform(1 - spread, 1 + spread),
            })

    def tick(self, now: float):
        for pack in self.packs:
            at = pack["next_at"]
            if at > now:
                continue
            for epc in pack["epcs"]:
                offset = self.rng.uniform(0, self.pack_gap) if len(pack["epcs"]) > 1 else 0.0
                self.device.place_tag(epc, rssi=self.rng.uniform(-58, -38), seconds=self.on_mat_seconds,
                                      at=at + offset)
                self.crossings[epc] += 1
            pack["next_at"] = at + pack["pace"] * self.rng.uniform(0.95, 1.05)


def capture_chunks(path: str) -> Iterator[Tuple[float, bytes]]:
    """(seconds from the first rx, bytes) for each rx line of a capture file (issue #51)."""
    from hardware.frame_capture import iter_capture
    first = None
    with open(path, encoding="ascii") as f:
        for kind, mono, text in iter_capture(f):
            if kind != "rx":
                continue
            first = mono if first is None else first
            yield mono - first, bytes.fromhex(text)


# --- virtual serial port (pty) ---
def open_pty() -> Tuple[int, int, str]:
    """A pseudo-terminal pair in raw mode; the slave path works as a serial port for pyserial."""
    import tty
    master, slave = os.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    return master, slave, os.ttyname(slave)


def _write_out(master: int, data: bytes):
    try:
        os.write(master, data)
    except BlockingIOError:
        pass  # nobody is reading the port: bytes are lost, as on a real UART


def serve_pty(device: FakeR200, runners: Optional[LappingRunners] = None,
              hang_after: Optional[float] = None, stop: Optional[threading.Event] = None,
              announce: Callable[[str], None] = print):
    master, slave, path = open_pty()
    announce(path)
    started = time.monotonic()
    last_report = started
    stop = stop or threading.Event()
    try:
        while not stop.is_set():
            ready, _, _ = select.select([master], [], [], 0.02)
            if ready:
                try:
                    device.receive(os.read(master, 1024))
                except (BlockingIOError, OSError):
                    pass
            now = time.monotonic()
            if runners:
                runners.tick(now)
            if hang_after is not None and not device.hung and now - started >= hang_after:
                device.hang()
                announce("# fake R200 hung (only a restart of this program brings it back)")
            out = device.output()
            if out:
                _write_out(master, out)
            if now - last_report >= 30:
                last_report = now
                state = "hung" if device.hung else ("scanning" if device.scanning else "idle")
                announce(f"# {state}: {device.polls} polls, {device.tag_frames} tag reads, "
                         f"{device.arms} arms, {device.stops_at_limit} stops at the poll limit")
    finally:
        os.close(master)
        os.close(slave)


def replay_pty(path: str, speed: float = 1.0, loop: bool = False, announce: Callable[[str], None] = print):
    """Plays a capture's received bytes on its original timing, ignoring commands."""
    chunks = list(capture_chunks(path))
    if not chunks:
        raise SystemExit(f"No rx lines in {path}")
    master, slave, port = open_pty()
    announce(port)
    try:
        while True:
            start = time.monotonic()
            for offset, data in chunks:
                delay = start + offset / speed - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                try:
                    while os.read(master, 1024):
                        pass  # discard commands from the host
                except (BlockingIOError, OSError):
                    pass
                _write_out(master, data)
            if not loop:
                break
        announce("# replay finished")
        time.sleep(1.0)  # let the host read the tail
    finally:
        os.close(master)
        os.close(slave)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Fake R200 on a virtual serial port. Point the app at the printed path: "
                    "MOCK_HARDWARE=false SERIAL_PORT=<path> python -m uvicorn web.app:app")
    p.add_argument("--runners", type=int, default=5, help="runners lapping the mat (0 = none)")
    p.add_argument("--epc", action="append", help="tag EPC to use (repeat); default: the demo runners' tags")
    p.add_argument("--lap", type=float, default=75.0, help="average lap time, seconds")
    p.add_argument("--poll-limit", type=int, help="stop scanning after this many polls (default: as commanded, 65,535)")
    p.add_argument("--corrupt", type=float, default=0.0, help="chance a frame has a corrupted byte (0-1)")
    p.add_argument("--drop", type=float, default=0.0, help="chance a frame is lost (0-1)")
    p.add_argument("--noise", type=float, default=0.0, help="chance of junk bytes before a frame (0-1)")
    p.add_argument("--rssi-jitter", type=float, default=0.0, help="RSSI noise, dB standard deviation")
    p.add_argument("--hang-after", type=float, help="seconds until the module locks up for good")
    p.add_argument("--replay", metavar="CAPTURE", help="replay a raw capture file instead (RAW_CAPTURE_PATH)")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed")
    p.add_argument("--loop", action="store_true", help="replay in a loop")
    p.add_argument("--seed", type=int)
    args = p.parse_args(argv)

    if os.name != "posix":
        raise SystemExit("The virtual serial port needs macOS or Linux.")

    def announce(text):
        if text.startswith("/"):
            print(f"Fake R200 listening on {text}\n"
                  f"  run the app:  MOCK_HARDWARE=false SEED_DEMO=true SERIAL_PORT={text} python -m uvicorn web.app:app\n"
                  f"  or sniff it:  python -m hardware.sniffer --port {text}", flush=True)
        else:
            print(text, flush=True)

    try:
        if args.replay:
            replay_pty(args.replay, speed=args.speed, loop=args.loop, announce=announce)
            return
        device = FakeR200(poll_limit=args.poll_limit, seed=args.seed,
                          faults=Faults(corrupt=args.corrupt, drop=args.drop, noise=args.noise,
                                        rssi_jitter=args.rssi_jitter))
        if args.epc:
            epcs = [e.upper() for e in args.epc]
        elif args.runners <= len(DEMO_EPCS):
            epcs = DEMO_EPCS[:args.runners]
        else:  # beyond the 5 demo runners the tags are unregistered: register them in /prepare
            epcs = DEMO_EPCS + [f"E28011700000020102{i:06d}" for i in range(1, args.runners - len(DEMO_EPCS) + 1)]
        runners = LappingRunners(device, epcs, lap_seconds=args.lap, seed=args.seed) if epcs else None
        serve_pty(device, runners, hang_after=args.hang_after, announce=announce)
    except KeyboardInterrupt:
        print(file=sys.stderr)


if __name__ == "__main__":
    main()
