#!/usr/bin/env python3
"""
Live RFID tag sniffer for M100/R200.
Continuously polls and prints every tag read with EPC, RSSI, and a visual bar.
Press Ctrl+C to stop.
"""
import serial
import time
import sys
import os
from collections import defaultdict

PORT  = os.environ.get("SERIAL_PORT", "/dev/cu.usbserial-130")
BAUD  = int(os.environ.get("BAUD_RATE", "115200"))

# M100/R200 commands
MULTI_POLL = bytes([0xAA, 0x00, 0x27, 0x00, 0x03, 0x22, 0xFF, 0xFF, 0x4A, 0xDD])
STOP_CMD   = bytes([0xAA, 0x00, 0x28, 0x00, 0x00, 0x28, 0xDD])

HEADER = 0xAA
FOOTER = 0xDD
TYPE_NOTIFICATION = 0x02  # tag read notification frame

def rssi_bar(rssi_raw):
    """Convert raw RSSI byte to signed dBm and a visual bar."""
    # RSSI byte is signed: values > 127 are negative (two's complement)
    if rssi_raw > 127:
        dbm = rssi_raw - 256
    else:
        dbm = rssi_raw
    # Typical range: -80 (weak) to -20 (strong)
    clamped = max(-80, min(-20, dbm))
    pct = (clamped + 80) / 60.0  # 0.0 to 1.0
    bar_len = int(pct * 20)
    bar = "█" * bar_len + "░" * (20 - bar_len)
    strength = "WEAK" if pct < 0.33 else ("GOOD" if pct < 0.66 else "STRONG")
    color = "\033[91m" if pct < 0.33 else ("\033[93m" if pct < 0.66 else "\033[92m")
    reset = "\033[0m"
    return dbm, f"{color}[{bar}]{reset} {dbm:4d} dBm  {color}{strength}{reset}"

def parse_frames(buf):
    """Extract complete frames from a byte buffer. Returns (frames, remaining_buf)."""
    frames = []
    i = 0
    while i < len(buf):
        # Find frame header
        if buf[i] != HEADER:
            i += 1
            continue
        if i + 4 >= len(buf):
            break  # not enough bytes yet
        frame_type = buf[i+1]
        # cmd        = buf[i+2]
        # param      = buf[i+3]
        payload_len = (buf[i+4] << 8 | buf[i+5]) if i+5 < len(buf) else None
        if payload_len is None:
            break
        frame_end = i + 6 + payload_len + 1 + 1  # header(6) + payload + checksum + footer
        if frame_end > len(buf):
            break  # incomplete frame
        footer = buf[frame_end - 1]
        if footer != FOOTER:
            i += 1
            continue
        frame = buf[i:frame_end]
        frames.append((frame_type, frame))
        i = frame_end
    return frames, buf[i:]

def extract_epc(frame):
    """
    Notification frame layout (type=0x02):
      AA 02 27 00 <len_hi> <len_lo> <payload...> <checksum> DD
    Payload for tag read:
      byte 0: RSSI (signed)
      byte 1: PC high
      byte 2: PC low
      bytes 3..N-2: EPC (N-2 bytes, typically 12)
      last byte: antenna port
    """
    payload_len = (frame[4] << 8) | frame[5]
    if payload_len < 4:
        return None, None
    payload = frame[6: 6 + payload_len]
    rssi_raw = payload[0]
    # EPC is bytes 3 to payload_len-1 (last byte = antenna port)
    epc_bytes = payload[3: payload_len - 1]
    epc_hex = epc_bytes.hex().upper()
    return rssi_raw, epc_hex

def main():
    print(f"\033[1;36m{'═'*58}\033[0m")
    print(f"\033[1;36m  🏷  M100/R200 Live RFID Sniffer  |  {PORT}\033[0m")
    print(f"\033[1;36m{'═'*58}\033[0m")
    print(f"  Baud: {BAUD}  |  Hold tag near antenna  |  Ctrl+C to quit")
    print(f"\033[1;36m{'─'*58}\033[0m\n")

    try:
        s = serial.Serial(PORT, BAUD, timeout=0.1)
    except serial.SerialException as e:
        print(f"\033[91mERROR: {e}\033[0m")
        sys.exit(1)

    time.sleep(0.3)
    s.write(MULTI_POLL)

    buf = b""
    seen_epcs = defaultdict(lambda: {"count": 0, "last_rssi": 0, "first_seen": time.time()})
    total_reads = 0

    print(f"  {'EPC':24s}  {'RSSI Bar':40s}  {'Reads':>5s}  {'Last Seen'}")
    print(f"  {'─'*24}  {'─'*40}  {'─'*5}  {'─'*12}")

    try:
        while True:
            chunk = s.read(512)
            if chunk:
                buf += chunk
                frames, buf = parse_frames(buf)
                for ftype, frame in frames:
                    if ftype == TYPE_NOTIFICATION:
                        rssi_raw, epc = extract_epc(frame)
                        if epc and len(epc) >= 8:
                            total_reads += 1
                            dbm, bar = rssi_bar(rssi_raw)
                            rec = seen_epcs[epc]
                            rec["count"] += 1
                            rec["last_rssi"] = dbm
                            now_str = time.strftime("%H:%M:%S")
                            # Clear line and reprint
                            print(f"\r  \033[1;37m{epc:<24s}\033[0m  {bar}  {rec['count']:>5d}  {now_str}")
    except KeyboardInterrupt:
        pass
    finally:
        s.write(STOP_CMD)
        time.sleep(0.1)
        s.close()
        print(f"\n\n\033[1;36m{'─'*58}\033[0m")
        print(f"  Session complete. Total tag reads: {total_reads}")
        print(f"  Unique tags seen: {len(seen_epcs)}")
        for epc, rec in seen_epcs.items():
            dbm, bar = rssi_bar(256 + rec['last_rssi'] if rec['last_rssi'] < 0 else rec['last_rssi'])
            print(f"    {epc}  last={rec['last_rssi']} dBm  reads={rec['count']}")
        print(f"\033[1;36m{'═'*58}\033[0m\n")

if __name__ == "__main__":
    main()
