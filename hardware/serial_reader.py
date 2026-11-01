import time
import threading
import logging
from typing import Callable, Optional
import serial
from core.models import TagRead
from hardware.base import BaseRFIDReader
from hardware.r200_protocol import (
    HEADER, FOOTER, READ_MULTI_COMMAND, READ_STOP_COMMAND, parse_inventory_response
)

logger = logging.getLogger("SerialReader")

class SerialReader(BaseRFIDReader):
    """
    Connects to the R200 UHF RFID module over USB-UART serial.
    Sends real-time inventory scan commands and processes incoming tag notifications.
    """
    def __init__(self, port: str = "/dev/ttyUSB0", baud_rate: int = 115200, on_tag_read: Callable[[TagRead], None] = None):
        super().__init__(on_tag_read)
        self.port = port
        self.baud_rate = baud_rate
        self.serial_conn: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

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
        self._stop_event.set()
        
        # Send stop inventory command if open
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.write(READ_STOP_COMMAND)
                self.serial_conn.flush()
                self.serial_conn.close()
            except Exception as e:
                logger.error(f"Error stopping inventory command: {e}")

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("R200 Serial Reader stopped.")

    def _read_loop(self):
        buffer = bytearray()
        while not self._stop_event.is_set():
            # Connection management with automatic reconnection
            if not self.serial_conn or not self.serial_conn.is_open:
                try:
                    self.serial_conn = serial.Serial(
                        port=self.port,
                        baudrate=self.baud_rate,
                        timeout=0.1
                    )
                    logger.info(f"Connected to R200 RFID reader on {self.port}")
                    # Send continuous multi-polling inventory command
                    self.serial_conn.write(READ_MULTI_COMMAND)
                    self.serial_conn.flush()
                except Exception as e:
                    logger.warning(f"Could not connect to {self.port}: {e}. Retrying in 2 seconds...")
                    time.sleep(2.0)
                    continue

            try:
                data = self.serial_conn.read(256)
                if data:
                    buffer.extend(data)
                    buffer = self._process_stream(buffer)
                else:
                    time.sleep(0.01)
            except Exception as e:
                logger.error(f"Serial read error on {self.port}: {e}")
                if self.serial_conn:
                    try:
                        self.serial_conn.close()
                    except Exception:
                        pass
                self.serial_conn = None
                time.sleep(1.0)

    def _process_stream(self, buffer: bytearray) -> bytearray:
        """Finds valid frames starting with 0xAA and ending with 0xDD."""
        while len(buffer) > 0:
            # Find frame header (0xAA)
            start_idx = -1
            for i, b in enumerate(buffer):
                if b == HEADER:
                    start_idx = i
                    break

            if start_idx == -1:
                return bytearray()  # No header found, clear buffer

            if start_idx > 0:
                buffer = buffer[start_idx:]  # Discard preceding garbage

            if len(buffer) < 5:
                break  # Wait for more bytes to read length

            length = (buffer[3] << 8) | buffer[4]
            total_frame_len = 5 + length + 2  # header(1) + type(1) + cmd(1) + len(2) + payload + cs(1) + footer(1)

            if len(buffer) < total_frame_len:
                break  # Wait for complete frame

            frame = bytes(buffer[:total_frame_len])
            buffer = buffer[total_frame_len:]

            parsed = parse_inventory_response(frame)
            if parsed:
                epc_hex, rssi = parsed
                tag_read = TagRead(epc=epc_hex, timestamp=time.time(), rssi=rssi)
                if self.on_tag_read:
                    self.on_tag_read(tag_read)

        return buffer
