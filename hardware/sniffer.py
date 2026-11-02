#!/usr/bin/env python3
"""
Live RFID tag sniffer for M100/R200.
Continuously polls and prints every tag read with EPC, RSSI, and a visual bar.
Uses the same frame parser as the race server, so what you see here is what the app sees.

  python -m hardware.sniffer           # from the project root
  python -m hardware.sniffer --raw     # also print each raw frame as hex (for verifying the protocol)

Press Ctrl+C to stop.
"""
import argparse
import os
import sys
import time
from collections import defaultdict

import serial

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hardware.r200_protocol import (  # noqa: E402
    READ_MULTI_COMMAND, READ_STOP_COMMAND, extract_frames, parse_tag_frame, raw_rssi_to_dbm
)

PORT = os.environ.get("SERIAL_PORT", "/dev/cu.usbserial-130")
BAUD = int(os.environ.get("BAUD_RATE", "115200"))


def rssi_bar(dbm: float) -> str:
    """Visual bar for a dBm value. Typical range: -80 (weak) to -20 (strong)."""
    pct = (max(-80.0, min(-20.0, dbm)) + 80) / 60.0
    bar_len = int(pct * 20)
    bar = "█" * bar_len + "░" * (20 - bar_len)
    strength = "WEAK" if pct < 0.33 else ("GOOD" if pct < 0.66 else "STRONG")
    color = "\033[91m" if pct < 0.33 else ("\033[93m" if pct < 0.66 else "\033[92m")
    return f"{color}[{bar}]\033[0m {dbm:6.1f} dBm  {color}{strength}\033[0m"


def main():
    parser = argparse.ArgumentParser(description="Live R200 tag sniffer")
    parser.add_argument("--raw", action="store_true", help="print every received frame as hex")
    args = parser.parse_args()

    print(f"\033[1;36m{'═'*58}\033[0m")
    print(f"\033[1;36m  🏷  M100/R200 Live RFID Sniffer  |  {PORT}\033[0m")
    print(f"\033[1;36m{'═'*58}\033[0m")
    print(f"  Baud: {BAUD}  |  Hold tag near antenna  |  Ctrl+C to quit")
    print(f"\033[1;36m{'─'*58}\033[0m\n", flush=True)

    try:
        s = serial.Serial(PORT, BAUD, timeout=0.1)
    except serial.SerialException as e:
        print(f"\033[91mERROR: {e}\033[0m")
        sys.exit(1)

    time.sleep(0.3)
    s.write(READ_MULTI_COMMAND)

    buf = bytearray()
    seen_epcs = defaultdict(lambda: {"count": 0, "last_rssi": 0.0})
    total_reads = 0

    print(f"  {'EPC':24s}  {'RSSI Bar':40s}  {'Reads':>5s}  {'Time'}")
    print(f"  {'─'*24}  {'─'*40}  {'─'*5}  {'─'*12}", flush=True)

    try:
        while True:
            chunk = s.read(512)
            if not chunk:
                continue
            buf.extend(chunk)
            frames, buf = extract_frames(buf)
            for frame in frames:
                if args.raw:
                    print(f"  raw: {frame.hex(' ').upper()}", flush=True)
                parsed = parse_tag_frame(frame)
                if not parsed:
                    continue  # command response or non-tag frame
                epc, raw_rssi = parsed
                dbm = raw_rssi_to_dbm(raw_rssi)
                total_reads += 1
                rec = seen_epcs[epc]
                rec["count"] += 1
                rec["last_rssi"] = dbm
                print(f"  \033[1;37m{epc:<24s}\033[0m  {rssi_bar(dbm)}  {rec['count']:>5d}  "
                      f"{time.strftime('%H:%M:%S')}  (raw RSSI byte: {raw_rssi})", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        s.write(READ_STOP_COMMAND)
        time.sleep(0.1)
        s.close()
        print(f"\n\n\033[1;36m{'─'*58}\033[0m")
        print(f"  Session complete. Total tag reads: {total_reads}")
        print(f"  Unique tags seen: {len(seen_epcs)}")
        for epc, rec in seen_epcs.items():
            print(f"    {epc}  last={rec['last_rssi']:.1f} dBm  reads={rec['count']}")
        print(f"\033[1;36m{'═'*58}\033[0m\n")


if __name__ == "__main__":
    main()
