"""
Opt-in log of the raw bytes exchanged with the R200 (issue #51).

The sniffer needs the serial port to itself; this records the same traffic from inside the
running server, so frames can be captured (issue #7) or a soak test run (issue #5) without
stopping the app. One line per event:

    2026-11-15T20:31:01.682Z 5321.104 rx AA022200113...DD         bytes as read from the port
    2026-11-15T20:31:01.690Z 5321.112 tx AA0027000322FFFF4ADD    a command we sent
    2026-11-15T20:31:01.690Z 5321.112 # arm routine              a note (arm/re-arm and why)

The first time is wall-clock UTC (for people), the second is time.monotonic() (for working
out gaps, immune to clock changes). rx lines are raw chunks, not parsed frames, so a capture
still shows what arrived even if the frame layout is wrong; replay it with load_rx_bytes().
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Iterator, List, Optional, Tuple

logger = logging.getLogger("FrameCapture")

DEFAULT_MAX_MB = 50.0


class FrameCapture:
    def __init__(self, path: str, max_bytes: int = int(DEFAULT_MAX_MB * 1024 * 1024),
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.path = path
        self.max_bytes = max_bytes
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._file = None
        self.enabled = True
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._file = open(path, "a", encoding="ascii")
            self._size = self._file.tell()
        except OSError as e:
            logger.warning(f"Raw frame capture disabled: can't open {path}: {e}")
            self.enabled = False
            return
        logger.info(f"Capturing raw R200 traffic to {path} (cap {max_bytes / 1048576:.0f} MB)")
        self.note("capture started")

    def rx(self, data: bytes):
        self._write("rx", data.hex().upper())

    def tx(self, data: bytes):
        self._write("tx", data.hex().upper())

    def note(self, text: str):
        self._write("#", text)

    def close(self):
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
            self.enabled = False

    def _write(self, kind: str, text: str):
        if not self.enabled:
            return
        stamp = self._wall_clock().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        line = f"{stamp} {self._clock():.3f} {kind} {text}\n"
        with self._lock:
            if not self._file:
                return
            try:
                if self._size + len(line) > self.max_bytes:
                    self._file.write(f"{stamp} {self._clock():.3f} # capture stopped: size cap reached\n")
                    self._file.close()
                    self._file = None
                    self.enabled = False
                    logger.warning(f"Raw frame capture stopped: {self.path} reached its size cap")
                    return
                self._file.write(line)
                self._file.flush()  # a soak test may end with the power being pulled
                self._size += len(line)
            except OSError as e:
                # Never let the capture get in the way of timing
                logger.warning(f"Raw frame capture stopped: write to {self.path} failed: {e}")
                self.enabled = False
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None


def open_capture(path: Optional[str], max_mb: Optional[float] = None, **kw) -> Optional[FrameCapture]:
    """A FrameCapture for path, or None when capture is off (no path set)."""
    if not path:
        return None
    max_mb = DEFAULT_MAX_MB if max_mb is None else float(max_mb)
    return FrameCapture(path, max_bytes=int(max_mb * 1024 * 1024), **kw)


def iter_capture(lines) -> Iterator[Tuple[str, float, str]]:
    """Yields (kind, monotonic_time, text) for each line of a capture; text is hex for rx/tx."""
    for raw in lines:
        parts = raw.rstrip("\n").split(" ", 3)
        if len(parts) < 4:
            continue
        _wall, mono, kind, text = parts
        try:
            yield kind, float(mono), text
        except ValueError:
            continue


def load_rx_bytes(path: str) -> bytes:
    """All bytes received from the reader in a capture file, concatenated in order."""
    with open(path, encoding="ascii") as f:
        chunks: List[bytes] = [bytes.fromhex(text) for kind, _, text in iter_capture(f) if kind == "rx"]
    return b"".join(chunks)
