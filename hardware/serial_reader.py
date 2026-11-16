import time
import threading
import logging
from typing import Callable, Optional
import serial
from core.models import TagRead
from core import clock
from hardware.base import BaseRFIDReader
from hardware.frame_capture import FrameCapture
from hardware.r200_protocol import (
    READ_MULTI_COMMAND, READ_STOP_COMMAND, extract_frames, parse_inventory_response
)

logger = logging.getLogger("SerialReader")

# The multi-poll command asks for 0xFFFF = 65,535 polling rounds, after which the R200 most
# likely stops scanning on its own (issue #5). Re-arm well before that, and also whenever the
# module has gone quiet, in case it stopped for any other reason.
DEFAULT_REARM_INTERVAL = 300.0   # seconds between routine stop + restart of the inventory
DEFAULT_SILENCE_REARM = 30.0     # re-arm if no frame of any kind arrived for this long


class SerialReader(BaseRFIDReader):
    """
    Connects to the R200 UHF RFID module over USB-UART serial.
    Sends real-time inventory scan commands and processes incoming tag notifications.
    """
    def __init__(self, port: str = "/dev/ttyUSB0", baud_rate: int = 115200,
                 on_tag_read: Callable[[TagRead], None] = None,
                 rearm_interval: float = DEFAULT_REARM_INTERVAL,
                 silence_rearm: float = DEFAULT_SILENCE_REARM,
                 serial_factory: Callable[..., serial.Serial] = serial.Serial,
                 clock: Callable[[], float] = time.monotonic,
                 capture: Optional[FrameCapture] = None):
        super().__init__(on_tag_read)
        self.port = port
        self.baud_rate = baud_rate
        self.rearm_interval = rearm_interval
        self.silence_rearm = silence_rearm
        self._serial_factory = serial_factory
        self._clock = clock
        self.capture = capture  # optional raw traffic log (issue #51)
        self.serial_conn: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Watchdog state (monotonic clock, so wall-clock jumps don't matter)
        self.last_arm_at: float = 0.0
        self.last_frame_at: float = 0.0   # last frame actually received (0 = none yet)
        self._silence_from: float = 0.0   # the watchdog counts silence from here
        self._connected_at: float = 0.0
        self.rearm_count: int = 0
        self._silence_logged = False

        # Connection health for the admin page (issue #48)
        self.connected: bool = False
        self.connect_count: int = 0
        self.last_error: Optional[str] = None

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="R200SerialThread")
        self._thread.start()
        logger.info(f"R200 Serial Reader starting on {self.port} @ {self.baud_rate} baud...")

    def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        self.connected = False
        self._stop_event.set()

        # Send stop inventory command if open
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self._send(READ_STOP_COMMAND)
                self.serial_conn.flush()
                self.serial_conn.close()
            except Exception as e:
                logger.error(f"Error stopping inventory command: {e}")

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self.capture:
            self.capture.note("reader stopped")
        logger.info("R200 Serial Reader stopped.")

    # --- inventory control / watchdog ---
    def _send(self, command: bytes):
        self.serial_conn.write(command)
        if self.capture:
            self.capture.tx(command)

    def _arm(self, restart: bool = False, reason: str = "connect"):
        """Starts continuous inventory. With restart=True, stops the current one first."""
        if self.capture:
            self.capture.note(f"arm {reason}")
        if restart:
            self._send(READ_STOP_COMMAND)
        self._send(READ_MULTI_COMMAND)
        self.serial_conn.flush()
        now = self._clock()
        self.last_arm_at = now
        # Count silence from the (re)arm, so a quiet field doesn't re-arm on every pass. This
        # must not touch last_frame_at: the admin page shows real silence from that, and a
        # module that stopped answering has to turn it amber even though we keep re-arming.
        self._silence_from = max(self.last_frame_at, now)

    def _maybe_rearm(self) -> Optional[str]:
        """Re-sends the inventory command if it's due. Returns the reason, or None."""
        now = self._clock()
        if self.rearm_interval and now - self.last_arm_at >= self.rearm_interval:
            reason = "routine"
            logger.info(f"Re-arming R200 inventory (routine, every {self.rearm_interval:.0f}s)")
        elif self.silence_rearm and now - self._silence_from >= self.silence_rearm:
            reason = "silence"
            # A field with no tags may be silent for a long time: say so once per quiet spell
            log = logger.debug if self._silence_logged else logger.info
            log(f"No frames from the R200 for {now - self._heard_from():.0f}s; re-arming inventory")
            self._silence_logged = True
        else:
            return None
        self._arm(restart=True, reason=reason)
        self.rearm_count += 1
        return reason

    def _read_loop(self):
        buffer = bytearray()
        while not self._stop_event.is_set():
            # Connection management with automatic reconnection
            if not self.serial_conn or not self.serial_conn.is_open:
                try:
                    self.serial_conn = self._serial_factory(
                        port=self.port,
                        baudrate=self.baud_rate,
                        timeout=0.1
                    )
                    logger.info(f"Connected to R200 RFID reader on {self.port}")
                    buffer = bytearray()
                    self._connected_at = self._clock()
                    self._arm()
                    self.connected = True
                    self.connect_count += 1
                    self.last_error = None
                except Exception as e:
                    logger.warning(f"Could not connect to {self.port}: {e}. Retrying in 2 seconds...")
                    self.serial_conn = None
                    self.connected = False
                    self.last_error = self._describe_open_error(e)
                    self._stop_event.wait(2.0)
                    continue

            try:
                data = self.serial_conn.read(256)
                if data:
                    if self.capture:
                        self.capture.rx(data)
                    buffer.extend(data)
                    buffer = self._process_stream(buffer)
                else:
                    time.sleep(0.01)
                self._maybe_rearm()
            except Exception as e:
                logger.error(f"Serial read error on {self.port}: {e}")
                self.connected = False
                self.last_error = f"Read error: {e}"
                if self.capture:
                    self.capture.note(f"serial error: {e}")
                if self.serial_conn:
                    try:
                        self.serial_conn.close()
                    except Exception:
                        pass
                self.serial_conn = None
                self._stop_event.wait(1.0)

    def _describe_open_error(self, e: Exception) -> str:
        """Short, actionable text for the admin page instead of pyserial's nested message."""
        text = str(e)
        if "No such file" in text or "[Errno 2]" in text:
            return f"no device at {self.port} (is the reader plugged in?)"
        if "Permission denied" in text or "[Errno 13]" in text:
            return f"permission denied on {self.port} (container needs the serial device rules)"
        if "busy" in text.lower() or "[Errno 16]" in text:
            return f"{self.port} is in use by another program (e.g. the sniffer)"
        return f"can't open {self.port}: {text}"

    def _heard_from(self) -> float:
        return max(self.last_frame_at, self._connected_at)

    def health(self):
        """Connection details for the admin page (issue #48). Read counts come from the app."""
        now = self._clock()
        return {
            "kind": "serial",
            "port": self.port,
            "running": self.is_running,
            "connected": self.connected,
            # Since the last frame, or since connecting if none came on this connection
            "seconds_since_frame": round(now - self._heard_from(), 1) if self.connected else None,
            "reconnects": max(0, self.connect_count - 1),
            "rearms": self.rearm_count,
            "last_error": self.last_error,
        }

    def _process_stream(self, buffer: bytearray) -> bytearray:
        """Emits a TagRead for every complete tag frame in the buffer; returns the unparsed remainder."""
        frames, buffer = extract_frames(buffer)
        if frames:
            self.last_frame_at = self._silence_from = self._clock()
            self._silence_logged = False
        for frame in frames:
            parsed = parse_inventory_response(frame)
            if parsed:
                epc_hex, rssi = parsed
                tag_read = TagRead(epc=epc_hex, timestamp=clock.now(), rssi=rssi)
                if self.on_tag_read:
                    self.on_tag_read(tag_read)
        return buffer
