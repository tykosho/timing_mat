"""
MagicRF R200 / M100 UHF RFID Serial Protocol Parser
Protocol format based on official R200 SDK:
[Header: 0xAA] [Type: 0x00=Cmd, 0x01=Resp, 0x02=Notify] [Command] [Length MSB] [Length LSB] [Payload...] [Checksum] [End: 0xDD]
"""
import logging
from typing import Optional, Tuple

logger = logging.getLogger("R200Protocol")

HEADER = 0xAA
FOOTER = 0xDD

# R200 Commands from SDK
CMD_HARDWARE_VERSION = 0x03
CMD_SINGLE_POLLING = 0x22
CMD_MULTI_POLLING = 0x27       # Read continuously until stopped
CMD_STOP_MULTI = 0x28          # Stop continuous reading
CMD_SET_POWER = 0xB6           # Set RF transmitting power
CMD_GET_POWER = 0xB7

def calculate_checksum(data: bytes) -> int:
    """Checksum: 8-bit sum of all bytes between Header and Checksum (modulo 256)."""
    return sum(data) & 0xFF

def build_command_frame(cmd: int, payload: bytes = b"") -> bytes:
    """Builds standard frame: [0xAA, 0x00, CMD, LEN_MSB, LEN_LSB, PAYLOAD, CS, 0xDD]"""
    frame_type = 0x00  # 0x00 = Host to Module
    length = len(payload)
    len_msb = (length >> 8) & 0xFF
    len_lsb = length & 0xFF

    body = bytes([frame_type, cmd, len_msb, len_lsb]) + payload
    cs = calculate_checksum(body)
    return bytes([HEADER]) + body + bytes([cs, FOOTER])

# Pre-built standard commands
READ_MULTI_COMMAND = bytes([0xAA, 0x00, 0x27, 0x00, 0x03, 0x22, 0xFF, 0xFF, 0x4A, 0xDD])
READ_STOP_COMMAND = bytes([0xAA, 0x00, 0x28, 0x00, 0x00, 0x28, 0xDD])
READ_SINGLE_COMMAND = bytes([0xAA, 0x00, 0x22, 0x00, 0x00, 0x22, 0xDD])

def parse_inventory_response(frame: bytes) -> Optional[Tuple[str, float]]:
    """
    Parses R200 notification frame (0xAA 0x02 0x22 ...):
      - frame[0]: 0xAA (Header)
      - frame[1]: 0x02 (Notification)
      - frame[2]: 0x22 (Tag read)
      - frame[3..4]: Length
      - frame[5]: RSSI raw byte
      - frame[6..7]: PC (Protocol Control, 2 bytes)
      - frame[8..8+epc_len]: EPC (typically 12 bytes = 24 hex characters)
      - frame[-2]: Checksum
      - frame[-1]: 0xDD (Footer)
    """
    if len(frame) < 10:
        return None

    if frame[0] != HEADER or frame[-1] != FOOTER:
        return None

    # frame[1] must be 0x02 (notification) and frame[2] must be 0x22 (tag read)
    if frame[1] != 0x02 or frame[2] != CMD_SINGLE_POLLING:
        return None

    try:
        length = (frame[3] << 8) | frame[4]
        if len(frame) < 5 + length + 2:
            return None

        # Verify checksum: sum of bytes from index 1 to len(frame)-3
        expected_cs = calculate_checksum(frame[1:-2])
        actual_cs = frame[-2]
        if expected_cs != actual_cs:
            logger.debug(f"Checksum mismatch: expected 0x{expected_cs:02X}, got 0x{actual_cs:02X}")
            # Still attempt parsing if frame structure is intact

        raw_rssi = frame[5]
        # Invert/offset to realistic dBm
        rssi_dbm = -129.0 + raw_rssi if raw_rssi > 0 else -55.0

        # Protocol Control bytes give word length (PC bits 15..11 = EPC word count)
        pc_msb = frame[6]
        epc_words = (pc_msb >> 3) & 0x1F
        epc_bytes_count = epc_words * 2

        if epc_bytes_count <= 0 or epc_bytes_count > 64:
            # Standard Gen2 default: 12 bytes (96-bit EPC)
            epc_bytes_count = min(12, length - 3)

        epc_bytes = frame[8:8 + epc_bytes_count]
        epc_hex = epc_bytes.hex().upper()
        return epc_hex, round(rssi_dbm, 1)
    except Exception as e:
        logger.debug(f"Frame parsing error: {e}")
        return None
