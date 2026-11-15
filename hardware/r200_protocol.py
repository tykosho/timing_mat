"""
MagicRF R200 / M100 UHF RFID Serial Protocol Parser
Protocol format based on official R200 SDK:
[Header: 0xAA] [Type: 0x00=Cmd, 0x01=Resp, 0x02=Notify] [Command] [Length MSB] [Length LSB] [Payload...] [Checksum] [End: 0xDD]
"""
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger("R200Protocol")

HEADER = 0xAA
FOOTER = 0xDD
MAX_PAYLOAD_LEN = 255  # tag/response payloads are well under this; larger means a false header

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

def raw_rssi_to_dbm(raw_rssi: int) -> float:
    """Converts the raw RSSI byte from an R200 tag-read frame to dBm.

    Per R200 user protocol V2.3.3 (pp. 41-42): the RSSI byte is a two's-complement
    signed 8-bit integer directly in dBm. No offset arithmetic needed.
    SDK calibration points: 0xBA = -70 dBm (noise floor), 0xF2 = -14 dBm.
    Verified on real hardware: 0xD9 = -39 dBm (tag held on antenna).
    """
    return float(raw_rssi if raw_rssi < 128 else raw_rssi - 256)

def extract_frames(buffer: bytearray) -> Tuple[List[bytes], bytearray]:
    """
    Splits a serial byte buffer into complete frames.
    Frame layout: [0xAA] [type] [cmd] [len MSB] [len LSB] [payload...] [checksum] [0xDD]
    Returns (frames, remaining_buffer). Bytes that can't start a valid frame are discarded.
    """
    frames: List[bytes] = []
    while buffer:
        start = buffer.find(HEADER)
        if start == -1:
            return frames, bytearray()
        if start > 0:
            del buffer[:start]

        if len(buffer) < 5:
            break  # need the length field

        payload_len = (buffer[3] << 8) | buffer[4]
        if payload_len > MAX_PAYLOAD_LEN:
            del buffer[:1]  # implausible length: a stray 0xAA, not a real header
            continue

        total_len = 5 + payload_len + 2
        if len(buffer) < total_len:
            break  # wait for the rest of the frame

        if buffer[total_len - 1] != FOOTER:
            del buffer[:1]  # false header byte; resync on the next 0xAA
            continue

        frames.append(bytes(buffer[:total_len]))
        del buffer[:total_len]
    return frames, buffer

def parse_tag_frame(frame: bytes) -> Optional[Tuple[str, int]]:
    """
    Parses an R200 tag notification frame (0xAA 0x02 0x22 ...) into (epc_hex, raw_rssi):
      - frame[0]: 0xAA (Header)
      - frame[1]: 0x02 (Notification)
      - frame[2]: 0x22 (Tag read)
      - frame[3..4]: Payload length
      - frame[5]: RSSI raw byte
      - frame[6..7]: PC (Protocol Control, 2 bytes)
      - frame[8..8+epc_len]: EPC (typically 12 bytes = 24 hex characters)
      - then 2 bytes CRC, 1 byte checksum, 0xDD footer
    Returns None for anything that isn't a well-formed tag read.
    """
    if len(frame) < 10 or frame[0] != HEADER or frame[-1] != FOOTER:
        return None
    if frame[1] != 0x02 or frame[2] != CMD_SINGLE_POLLING:
        return None

    length = (frame[3] << 8) | frame[4]
    if len(frame) != 5 + length + 2 or length < 5:
        return None

    # Checksum and PC are verified leniently (logged, not rejected): the frame structure
    # is already validated above, and these rules haven't been confirmed on real hardware.
    if calculate_checksum(frame[1:-2]) != frame[-2]:
        logger.debug("Tag frame checksum mismatch")

    # Payload = RSSI(1) + PC(2) + EPC(n) + CRC(2)
    epc_len = length - 5
    pc_bytes = ((frame[6] >> 3) & 0x1F) * 2  # PC bits 15..11 = EPC length in 16-bit words
    if pc_bytes != epc_len:
        logger.debug(f"PC says EPC is {pc_bytes} bytes but frame length implies {epc_len}")
        if 0 < pc_bytes <= epc_len:
            epc_len = pc_bytes

    return frame[8:8 + epc_len].hex().upper(), frame[5]

def parse_inventory_response(frame: bytes) -> Optional[Tuple[str, float]]:
    """Parses a tag notification frame into (epc_hex, rssi_dbm)."""
    parsed = parse_tag_frame(frame)
    if not parsed:
        return None
    epc_hex, raw_rssi = parsed
    return epc_hex, round(raw_rssi_to_dbm(raw_rssi), 1)
