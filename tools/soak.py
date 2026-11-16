"""
Soak test: a whole race on a laptop (issue #69), through the fake R200 (#68).

    python -m tools.soak fast [--hours 3] [--runners 60]
        The race runs on a simulated clock in this process: fake R200 bytes -> SerialReader ->
        lap engine -> SQLite. Three race hours take a few minutes. Every lap is checked against
        what the simulated runners actually did; memory, database size and leaderboard query
        time are reported per race hour.

    python -m tools.soak live [--minutes 130] [--runners 60] [--clients 40]
        Real time, against the real app: uvicorn in physical mode on the fake reader's virtual
        serial port, with many WebSocket clients like phones and the TV. Every minute it samples
        the server's memory (RSS), open files, database + WAL size, leaderboard latency, reader
        health and messages per client, into a CSV.

Both exit non-zero if a lap was missed or invented, or a tag nobody wears showed up.
"""
import argparse
import asyncio
import functools
import csv
import http.cookiejar
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.request
from typing import Any, Dict, List, Optional
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import clock as race_clock  # noqa: E402
from core.db import Database  # noqa: E402
from core.lap_engine import LapEngine  # noqa: E402
from hardware.fake_r200 import FakeR200, Faults, LappingRunners, serve_pty  # noqa: E402
from hardware.serial_reader import SerialReader  # noqa: E402

MIN_LAP_SECONDS = 45.0


def race_epcs(n: int) -> List[str]:
    return [f"E2801170000002019999{i:04d}" for i in range(1, n + 1)]


def make_runners(r200: FakeR200, epcs: List[str], lap_seconds: float, seed: int) -> List[LappingRunners]:
    """Half the field runs alone, the other half in packs of 4 that cross the mat together."""
    half = len(epcs) // 2
    groups = [LappingRunners(r200, epcs[:half], lap_seconds=lap_seconds, spread=0.33, seed=seed)]
    if epcs[half:]:
        groups.append(LappingRunners(r200, epcs[half:], lap_seconds=lap_seconds, spread=0.33,
                                     seed=seed + 1, pack_size=4))
    return groups


def soak_faults() -> Faults:
    return Faults(corrupt=0.05, drop=0.05, noise=0.02, rssi_jitter=3.0)


def mb(nbytes: float) -> float:
    return round(nbytes / 1048576, 2)


def db_bytes(path: str) -> int:
    return sum(os.path.getsize(p) for p in (path, path + "-wal") if os.path.exists(p))


def pct(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


PASS_GAP_SECONDS = 20.0  # reads further apart than this belong to different passes over the mat


def passes_seen(timestamps: List[float]) -> int:
    """How many passes over the mat produced at least one read that reached the app."""
    passes, last = 0, None
    for t in sorted(timestamps):
        if last is None or t - last > PASS_GAP_SECONDS:
            passes += 1
        last = t
    return passes


def check_laps(crossings: Dict[str, int], seen: Dict[str, int], laps_by_epc: Dict[str, int]):
    """Splits lap differences into app errors and reader misses.

    The first pass the app sees starts the runner; every later one is a lap. A pass with no
    read at all (a crowded mat, say) can't be counted by any software: that's a reader miss,
    reported but not a failure. Counting the passes it did see wrongly is an app error.
    """
    app_errors, reader_misses = [], []
    for epc, n in crossings.items():
        got = laps_by_epc.get(epc, 0)
        if got != max(0, seen.get(epc, 0) - 1):
            app_errors.append(f"{epc}: {got} laps counted from {seen.get(epc, 0)} passes seen")
        missed = n - seen.get(epc, 0)
        if missed:
            reader_misses.append(f"{epc}: {missed} of {n} passes never read "
                                 f"({got} laps counted, {max(0, n - 1)} run)")
    return app_errors, reader_misses


def seen_from_db(db_path: str) -> Dict[str, int]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        by_epc: Dict[str, List[float]] = {}
        for epc, t in conn.execute("SELECT epc, timestamp FROM raw_reads"):
            by_epc.setdefault(epc, []).append(t)
    finally:
        conn.close()
    return {epc: passes_seen(ts) for epc, ts in by_epc.items()}


# --- fast: simulated clock, one process ---
def run_fast(runners: int = 60, hours: float = 3.0, seed: int = 1, step: float = 0.2,
             lap_seconds: float = 90.0, report=functools.partial(print, flush=True), track_memory: bool = True) -> Dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="soak-")
    db_path = os.path.join(tmp, "race.db")
    clock = [1_800_000_000.0]
    now = lambda: clock[0]  # noqa: E731

    if track_memory:
        tracemalloc.start()
    try:
        with mock.patch.object(race_clock, "now", now):
            db = Database(db_path)
            epcs = race_epcs(runners)
            for bib, epc in enumerate(epcs, start=1):
                db.add_participant(bib, f"Runner {bib}", epc)
            bib_of = {epc: bib for bib, epc in enumerate(epcs, start=1)}
            db.update_event_settings(min_lap_seconds=MIN_LAP_SECONDS)
            db.start_event(at=now())

            r200 = FakeR200(clock=now, seed=seed, faults=soak_faults(), max_tags_per_poll=4)
            groups = make_runners(r200, epcs, lap_seconds, seed)
            unknown, too_soon, board_ms = [], [], []

            def on_lap(_lap):
                # What web/app.py does on every lap before broadcasting it
                t0 = time.perf_counter()
                db.get_leaderboard()
                board_ms.append((time.perf_counter() - t0) * 1000)

            engine = LapEngine(db=db, crossing_window=2.5, on_lap_completed=on_lap,
                               on_unassigned_tag=unknown.append, on_crossing_too_soon=too_soon.append)
            reader = SerialReader(on_tag_read=engine.process_tag_read, clock=now)
            # What SerialReader's thread does on connect, without the thread: the port is the
            # fake one, read without waiting, so the race runs as fast as the CPU allows
            reader.serial_conn = r200.serial_factory(timeout=0)
            reader.connected = True
            reader._connected_at = now()
            rows = []
            wall_start = time.time()

            async def race():
                buffer = bytearray()
                reader._arm()
                start = now()
                end = start + hours * 3600
                next_hour = start + 3600
                ticks = 0
                while now() < end:
                    clock[0] += step
                    ticks += 1
                    if now() < end - 30:  # let the last runners finish crossing
                        for g in groups:
                            g.tick(now())
                    buffer.extend(reader.serial_conn.read(1 << 20))
                    buffer = reader._process_stream(buffer)
                    reader._maybe_rearm()  # the routine re-arm every 300 s, as on the Pi
                    await engine.flush_once(now())
                    if ticks % 5 == 0:  # flush_loop does this every 0.2 s; once a second is enough here
                        engine.refresh_event()
                        engine.flush_raw_reads()
                    if now() >= next_hour or now() >= end:
                        next_hour += 3600
                        rows.append(sample(start))
                clock[0] += 10
                await engine.flush_once(now())
                engine.flush_raw_reads()

            def sample(start):
                lap_total = sum(r["total_laps"] for r in db.get_leaderboard())
                with db.connect() as conn:
                    raw = conn.execute("SELECT COUNT(*) FROM raw_reads").fetchone()[0]
                row = {
                    "race_hour": round((now() - start) / 3600, 2),
                    "laps": lap_total,
                    "raw_reads": raw,
                    "db_mb": mb(db_bytes(db_path)),
                    "py_mem_mb": mb(tracemalloc.get_traced_memory()[0]) if track_memory else None,
                    "board_p50_ms": round(pct(board_ms, 0.5), 2),
                    "board_p95_ms": round(pct(board_ms, 0.95), 2),
                    "board_max_ms": round(max(board_ms or [0]), 2),
                    "buffers": len(engine.buffers),
                    "rearms": reader.rearm_count,
                    "wall_s": round(time.time() - wall_start, 1),
                }
                board_ms.clear()
                report(json.dumps(row))
                return row

            asyncio.run(race())

            crossings: Dict[str, int] = {}
            for g in groups:
                crossings.update(g.crossings)
            board = {row["bib_number"]: row["total_laps"] for row in db.get_leaderboard()}
            laps_by_epc = {epc: board.get(bib_of[epc], 0) for epc in epcs}
            with db.connect() as conn:
                flagged = conn.execute("SELECT COUNT(*) FROM laps WHERE flag_reason IS NOT NULL "
                                       "AND flag_reason != ''").fetchone()[0]
            app_errors, reader_misses = check_laps(crossings, seen_from_db(db_path), laps_by_epc)
            result = {
                "mode": "fast", "runners": runners, "race_hours": hours, "rows": rows,
                "laps": sum(laps_by_epc.values()), "expected_laps": sum(max(0, n - 1) for n in crossings.values()),
                "app_errors": app_errors, "reader_misses": reader_misses,
                "unknown_tags": len(unknown), "too_soon_prompts": len(too_soon), "flagged_laps": flagged,
                "corrupted_frames": r200.corrupted, "dropped_frames": r200.dropped, "polls": r200.polls,
                "poll_limit_stops": r200.stops_at_limit,
            }
            result["ok"] = not app_errors and not unknown and not too_soon
            return result
    finally:
        if track_memory:
            tracemalloc.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# --- live: the real app, real time ---
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def process_rss_mb(pid: int) -> Optional[float]:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
        return round(int(out.stdout.strip()) / 1024, 1)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def open_files(pid: int) -> Optional[int]:
    if os.path.isdir(f"/proc/{pid}/fd"):
        return len(os.listdir(f"/proc/{pid}/fd"))
    if shutil.which("lsof"):
        out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True, timeout=30)
        return max(0, len(out.stdout.splitlines()) - 1)
    return None


class Http:
    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: Any = None, form: bool = False, timeout: float = 30):
        data, headers = None, {}
        if body is not None:
            if form:
                data = "&".join(f"{k}={v}" for k, v in body.items()).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with self.opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)


async def ws_client(url: str, stats: Dict[str, Any], stop: asyncio.Event, headers: Optional[Dict[str, str]] = None):
    import websockets
    while not stop.is_set():
        try:
            async with websockets.connect(url, additional_headers=headers, max_size=None,
                                          open_timeout=20, ping_interval=20) as ws:
                stats["connects"] += 1
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    stats["messages"] += 1
                    stats["bytes"] += len(msg)
                    kind = json.loads(msg).get("type")
                    stats["types"][kind] = stats["types"].get(kind, 0) + 1
        except Exception as e:  # dropped: count it and reconnect, like the pages do
            stats["errors"] += 1
            stats["last_error"] = repr(e)
            await asyncio.sleep(1.0)


def run_live(runners: int = 60, minutes: float = 130.0, clients: int = 40, seed: int = 1,
             lap_seconds: float = 90.0, csv_path: Optional[str] = None, sample_every: float = 60.0,
             report=functools.partial(print, flush=True)) -> Dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="soak-live-")
    db_path = os.path.join(tmp, "race.db")
    csv_path = csv_path or os.path.join(tmp, "soak.csv")
    pin = "482913"

    r200 = FakeR200(seed=seed, faults=soak_faults(), max_tags_per_poll=4)
    epcs = race_epcs(runners)
    groups: List[LappingRunners] = []
    ticking = threading.Event()

    class Field:
        """Hands serve_pty one tick() for all runner groups, once the race has started."""
        def tick(self, now):
            if ticking.is_set():
                for g in groups:
                    g.tick(now)

    ports: List[str] = []
    stop_pty = threading.Event()
    pty_thread = threading.Thread(target=serve_pty, args=(r200, Field()), daemon=True,
                                  kwargs={"stop": stop_pty, "announce": lambda s: ports.append(s) if s.startswith("/") else None})
    pty_thread.start()
    while not ports:
        time.sleep(0.05)

    port = free_port()
    env = dict(os.environ, MOCK_HARDWARE="false", SEED_DEMO="false", SERIAL_PORT=ports[0],
               DB_PATH=db_path, ADMIN_PIN=pin, PYTHONUNBUFFERED="1")
    log = open(os.path.join(tmp, "server.log"), "w")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1",
                               "--port", str(port), "--log-level", "warning"],
                              cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    http = Http(f"http://127.0.0.1:{port}")
    report(f"# server pid {server.pid} on :{port}, reader {ports[0]}, files in {tmp}")
    try:
        for _ in range(100):
            try:
                http.call("GET", "/api/leaderboard", timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        http.call("POST", "/login", {"pin": pin}, form=True)
        for bib, epc in enumerate(epcs, start=1):
            http.call("POST", "/api/participants", {"bib_number": bib, "name": f"Runner {bib}", "epc": epc})
        # min lap time: config.yaml's 45 s, the same MIN_LAP_SECONDS the fast mode uses
        http.call("POST", "/api/event/start")
        groups.extend(make_runners(r200, epcs, lap_seconds, seed))
        ticking.set()
        result = asyncio.run(_live_loop(http, port, server, db_path, csv_path, clients, minutes,
                                        sample_every, report, groups, ticking))
        board = {row["bib_number"]: row["total_laps"] for row in http.call("GET", "/api/leaderboard")}
        crossings: Dict[str, int] = {}
        for g in groups:
            crossings.update(g.crossings)
        laps_by_epc = {epc: board.get(bib, 0) for bib, epc in enumerate(epcs, start=1)}
        app_errors, reader_misses = check_laps(crossings, seen_from_db(db_path), laps_by_epc)
        result.update({
            "laps": sum(laps_by_epc.values()),
            "expected_laps": sum(max(0, n - 1) for n in crossings.values()),
            "app_errors": app_errors, "reader_misses": reader_misses,
            "corrupted_frames": r200.corrupted, "dropped_frames": r200.dropped,
            "csv": csv_path, "server_log": log.name,
        })
        result["ok"] = (not app_errors and not result["unknown_tags"]
                        and server.poll() is None)
        return result
    finally:
        stop_pty.set()
        if server.poll() is None:
            server.send_signal(signal.SIGINT)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        log.close()


async def _live_loop(http, port, server, db_path, csv_path, clients, minutes, sample_every, report,
                     groups, ticking) -> Dict[str, Any]:
    stop = asyncio.Event()
    public = [{"connects": 0, "messages": 0, "bytes": 0, "errors": 0, "types": {}, "last_error": None}
              for _ in range(clients)]
    admin = {"connects": 0, "messages": 0, "bytes": 0, "errors": 0, "types": {}, "last_error": None}
    url = f"ws://127.0.0.1:{port}/ws/live"
    tasks = [asyncio.create_task(ws_client(url, s, stop)) for s in public]
    tasks.append(asyncio.create_task(ws_client(url + "?channel=admin", admin, stop,
                                               headers={"Cookie": http.cookie_header()})))
    start = time.time()
    end = start + minutes * 60
    # No new crossings in the last 30 s, so the totals have settled when we compare them
    asyncio.get_running_loop().call_later(max(0.0, minutes * 60 - 30), ticking.clear)
    rows = []
    fields = ["minute", "rss_mb", "open_files", "db_mb", "laps", "board_ms", "page_ms", "health",
              "reads_per_s", "ws_msgs_per_client", "ws_kb_per_client", "ws_errors", "admin_msgs"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        while time.time() < end and server.poll() is None:
            await asyncio.sleep(min(sample_every, max(0.1, end - time.time())))
            t0 = time.perf_counter()
            board = await asyncio.to_thread(http.call, "GET", "/api/leaderboard")
            board_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            await asyncio.to_thread(http.call, "GET", "/leaderboard")
            page_ms = (time.perf_counter() - t0) * 1000
            health = await asyncio.to_thread(http.call, "GET", "/api/reader/health")
            row = {
                "minute": round((time.time() - start) / 60, 1),
                "rss_mb": process_rss_mb(server.pid),
                "open_files": await asyncio.to_thread(open_files, server.pid),
                "db_mb": mb(db_bytes(db_path)),
                "laps": sum(r["total_laps"] for r in board),
                "board_ms": round(board_ms, 1),
                "page_ms": round(page_ms, 1),
                "health": health.get("state"),
                "reads_per_s": health.get("reads_per_second"),
                "ws_msgs_per_client": round(statistics.mean(s["messages"] for s in public), 1),
                "ws_kb_per_client": round(statistics.mean(s["bytes"] for s in public) / 1024, 1),
                "ws_errors": sum(s["errors"] for s in public) + admin["errors"],
                "admin_msgs": admin["messages"],
            }
            writer.writerow(row)
            f.flush()
            rows.append(row)
            report(json.dumps(row))
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "mode": "live", "minutes": minutes, "clients": clients, "rows": rows,
        "server_alive": server.poll() is None,
        "unknown_tags": admin["types"].get("unassigned_tag_detected", 0),
        "too_soon_prompts": admin["types"].get("crossing_too_soon", 0),
        "ws_reconnects": sum(s["connects"] - 1 for s in public if s["connects"]),
        "ws_errors": sum(s["errors"] for s in public),
        "ws_last_error": next((s["last_error"] for s in public if s["last_error"]), None),
        "ws_message_types": admin["types"],
    }


def summarize(result: Dict[str, Any]) -> str:
    rows = result.get("rows") or []
    lines = [f"\nSOAK {result['mode'].upper()}: {'PASS' if result['ok'] else 'FAIL'}",
             f"  laps counted {result['laps']} / run {result['expected_laps']}; "
             f"unknown tags {result['unknown_tags']}; too-soon prompts {result['too_soon_prompts']}",
             f"  faults injected: {result['corrupted_frames']} corrupted, {result['dropped_frames']} dropped frames"]
    for problem in result["app_errors"][:10]:
        lines.append(f"  APP ERROR {problem}")
    lines.append(f"  reader misses (passes with no read at all; no software can count these): "
                 f"{len(result['reader_misses'])}")
    for miss in result["reader_misses"][:10]:
        lines.append(f"    {miss}")
    if rows:
        first, last = rows[0], rows[-1]
        for key in ("py_mem_mb", "rss_mb", "db_mb", "board_p95_ms", "board_ms", "open_files"):
            if first.get(key) is not None:
                lines.append(f"  {key}: {first[key]} -> {last[key]}")
    if result["mode"] == "live":
        lines.append(f"  ws reconnects {result['ws_reconnects']}, errors {result['ws_errors']}"
                     f"{' (' + result['ws_last_error'] + ')' if result['ws_last_error'] else ''}; "
                     f"server alive: {result['server_alive']}")
        lines.append(f"  csv: {result['csv']}  server log: {result['server_log']}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    f = sub.add_parser("fast", help="simulated clock, minutes for hours of race")
    f.add_argument("--hours", type=float, default=3.0)
    f.add_argument("--runners", type=int, default=60)
    f.add_argument("--lap", type=float, default=90.0, help="average lap time, seconds")
    f.add_argument("--seed", type=int, default=1)
    live = sub.add_parser("live", help="real time against the real app")
    live.add_argument("--minutes", type=float, default=130.0)
    live.add_argument("--runners", type=int, default=60)
    live.add_argument("--clients", type=int, default=40, help="WebSocket clients (phones, TV)")
    live.add_argument("--lap", type=float, default=90.0)
    live.add_argument("--seed", type=int, default=1)
    live.add_argument("--csv", help="where to write the per-minute samples")
    live.add_argument("--every", type=float, default=60.0, help="seconds between samples")
    args = p.parse_args(argv)

    if args.mode == "fast":
        result = run_fast(runners=args.runners, hours=args.hours, seed=args.seed, lap_seconds=args.lap)
    else:
        result = run_live(runners=args.runners, minutes=args.minutes, clients=args.clients, seed=args.seed,
                          lap_seconds=args.lap, csv_path=args.csv, sample_every=args.every)
    print(summarize(result), flush=True)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
