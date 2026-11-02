"""CSV exports for an event: final results, every lap, registrations, and the raw tag reads."""
import csv
import io
from datetime import datetime
from typing import Any, List, Optional

from core.db import Database

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: Any) -> Any:
    """Stops spreadsheet apps from running a text cell as a formula (names come from online forms)."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _to_csv(header: List[str], rows: List[List[Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_safe(v) for v in row])
    return out.getvalue()


def _iso(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds") if ts else ""


def _hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def results_csv(db: Database, event_id: Optional[int] = None) -> str:
    board = db.get_leaderboard(event_id)
    rows = []
    for rank, r in enumerate(board, start=1):
        laps = r["total_laps"]
        rows.append([
            rank, r["bib_number"], r["name"], laps, round(r["distance_m"] / 1000, 3),
            _hms(r["total_time_seconds"]), round(r["total_time_seconds"], 2),
            round(r["total_time_seconds"] / laps, 2) if laps else "",
            r["fastest_lap_seconds"] if r["fastest_lap_seconds"] is not None else "",
            r["last_lap_seconds"] if r["last_lap_seconds"] is not None else "",
            _hms(r["total_rest_seconds"]), _iso(r["start_time"]), r["status"],
        ])
    return _to_csv(
        ["rank", "bib", "name", "laps", "distance_km", "active_time", "active_seconds", "avg_lap_seconds",
         "fastest_lap_seconds", "last_lap_seconds", "rest_time", "start_time", "status"], rows)


def laps_csv(db: Database, event_id: Optional[int] = None) -> str:
    rows = [[l["bib_number"], l["name"] or "", l["lap_number"], _iso(l["crossing_time"]), l["crossing_time"],
             l["lap_duration_seconds"], l["peak_rssi"], "yes" if l["manual_entry"] else ""]
            for l in db.get_all_laps(event_id)]
    return _to_csv(["bib", "name", "lap", "crossing_time", "crossing_epoch", "lap_seconds", "peak_rssi", "manual"], rows)


def registrations_csv(db: Database, event_id: Optional[int] = None) -> str:
    rows = [[p["bib_number"], p["name"], p["email"] or "", p["epc"] or "", "yes" if p["epc"] else "no", p["notes"] or ""]
            for p in db.get_all_participants(event_id)]
    return _to_csv(["bib", "name", "email", "epc", "tagged", "notes"], rows)


def raw_reads_csv(db: Database, event_id: Optional[int] = None) -> str:
    rows = [[_iso(r["timestamp"]), r["timestamp"], r["epc"], r["rssi"]] for r in db.get_raw_reads(event_id)]
    return _to_csv(["time", "epoch", "epc", "rssi"], rows)
