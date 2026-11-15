"""
Command-line hardware message & glitch simulator.

Can either:
1. Run an in-process race simulation against LapEngine (`--demo`), showing how
   happy-path crossings and each glitch type (double_bounce, double_count,
   missed_read, weak_stray, loiter, pack_crossing, unknown_tag) are handled.
2. Control the running local web server over HTTP (`--scenario`, `--preset`).

Examples:
    python -m hardware.simulator_cli --demo
    python -m hardware.simulator_cli --scenario double_bounce --bib 101
    python -m hardware.simulator_cli --preset glitchy
"""
import argparse
import asyncio
import json
import os
import tempfile
import urllib.request
from typing import List

from core.db import Database
from core.lap_engine import LapEngine
from core.models import TagRead
from hardware.mock_reader import MockReader, SCENARIO_METADATA, SIMULATOR_PRESETS


async def _flush_burst_at(engine: LapEngine, epc: str):
    reads = engine.buffers.pop(epc, [])
    if reads:
        return await engine._resolve_crossing(epc, reads)
    return None


async def run_demo_walkthrough():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(db_path)
        db.seed_demo_runners()
        db.update_event_settings(min_lap_seconds=45.0, max_lap_rest_seconds=300.0)
        db.start_event(at=1000.0)

        events_seen: List[str] = []
        engine = LapEngine(
            db=db,
            crossing_window=2.5,
            on_runner_started=lambda d: events_seen.append(f"STARTED #{d['bib_number']} ({d['name']})"),
            on_lap_completed=lambda d: events_seen.append(
                f"LAP #{d['lap_number']} for #{d['bib_number']} ({d['name']}) in {d['lap_duration_seconds']:.1f}s"
            ),
        )
        engine.refresh_event()

        # Intercept reads so we can flush bursts deterministically when a gap > crossing_window occurs
        last_ts_by_epc = {}
        outcomes = []

        def on_read(read: TagRead):
            epc = read.epc
            prev_ts = last_ts_by_epc.get(epc)
            if prev_ts is not None and (read.timestamp - prev_ts) >= engine.crossing_window:
                # Previous burst finished before this read arrived
                reads = engine.buffers.pop(epc, [])
                if reads:
                    outcomes.append((epc, reads))
            last_ts_by_epc[epc] = read.timestamp
            engine.process_tag_read(read)

        sim = MockReader(on_tag_read=on_read, speed_multiplier=1.0)
        sim.participant_provider = db.get_all_participants
        sim.event_state_provider = db.get_event_state

        async def resolve_all_pending():
            for epc, reads in outcomes:
                await engine._resolve_crossing(epc, reads)
            outcomes.clear()
            for epc in list(engine.buffers.keys()):
                await _flush_burst_at(engine, epc)
            last_ts_by_epc.clear()

        print("=" * 78)
        print("  HARDWARE MESSAGE & GLITCH SIMULATOR — WALKTHROUGH")
        print("=" * 78)

        steps = [
            (1005.0, "mass_start", None, "All 5 runners start together in a tight pack"),
            (1065.0, "normal", 101, "Happy Path: #101 completes Lap 1 cleanly (60s)"),
            (1130.0, "double_bounce", 101, "Glitch: #101 crosses for Lap 2 (65s), then bounces back 3.7s later"),
            (1195.0, "missed_read", 101, "Glitch: #101 passes mat at t=1195s with occluded bib (0 reads)"),
            (1260.0, "normal", 101, "Recovery: #101 crosses at t=1260s -> records combined 2-lap duration (130s)"),
            (1325.0, "double_count", 101, "Glitch: #101 crosses at t=1325s AND premature crossing 46.5s later"),
            (1080.0, "loiter", 102, "Glitch: #102 crosses and loiters on mat for 4.5s (20 continuous reads)"),
            (1350.0, "unknown_tag", None, "Glitch: Unassigned stray RFID tag walks past antenna"),
        ]

        for t, scenario, bib, note in steps:
            events_seen.clear()
            ev = sim.run_scenario(scenario=scenario, bib=bib, blocking=False, base_time=t)
            await resolve_all_pending()
            print(f"\n[t={t:6.1f}s] {note}")
            print(f"  Simulator sent : [{ev['label']}] {ev['reads_emitted']} TagRead msg(s) — {ev['description']}")
            print(f"  Engine outcome : {', '.join(events_seen) if events_seen else '(No lap/start recorded — debounced or ignored)'}")

        print("\n" + "=" * 78)
        print("  FINAL LEADERBOARD STATE")
        print("=" * 78)
        for row in db.get_leaderboard():
            print(
                f"  #{row['bib_number']} {row['name']:<16} | Laps: {row['total_laps']} | "
                f"Last: {(row['last_lap_seconds'] or 0):5.1f}s | Best: {(row['fastest_lap_seconds'] or 0):5.1f}s | "
                f"Distance: {row['distance_m']:6.0f}m"
            )
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Hardware Message & Glitch Simulator CLI")
    parser.add_argument("--demo", action="store_true", help="Run deterministic walkthrough of happy path + all glitches")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Base URL of running server")
    parser.add_argument("--scenario", choices=list(SCENARIO_METADATA.keys()), help="Inject a scenario on the running server")
    parser.add_argument("--bib", type=int, default=101, help="Target bib number for scenario injection")
    parser.add_argument("--preset", choices=list(SIMULATOR_PRESETS.keys()), help="Set background simulation preset on server")
    args = parser.parse_args()

    if args.demo or (not args.scenario and not args.preset):
        asyncio.run(run_demo_walkthrough())
        return

    if args.preset:
        res = post_json(f"{args.url}/api/simulator/preset", {"preset": args.preset})
        print(f"Preset updated to '{res['preset']}' (auto={res['auto_crossing_enabled']})")

    if args.scenario:
        res = post_json(f"{args.url}/api/simulator/scenario", {"scenario": args.scenario, "bib": args.bib})
        print(f"Triggered scenario: {res}")


if __name__ == "__main__":
    main()
