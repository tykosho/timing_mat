"""Predict 5K race mode: shared-start countdown, spoiler-free finish capture,
1-second accuracy scoring with ties, and worst-to-first reveal sequence.
"""
import logging
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, List, Optional

from core import clock
from core.models import TagRead, epc_to_apriltag

logger = logging.getLogger("Predict5K")


def parse_predicted_time(raw: Any) -> int:
    """Parse 'MM:SS', 'HH:MM:SS', or integer seconds into whole seconds (> 0)."""
    if isinstance(raw, (int, float)):
        secs = int(round(float(raw)))
        if secs <= 0:
            raise ValueError("Predicted time must be greater than 0 seconds.")
        return secs
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Predicted time is required (e.g. 24:30).")
    if re.fullmatch(r"\d+", text):
        secs = int(text)
        if secs <= 0:
            raise ValueError("Predicted time must be greater than 0 seconds.")
        return secs
    parts = text.split(":")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        mins, sec = int(parts[0]), int(parts[1])
        if sec >= 60:
            raise ValueError("Seconds must be 00–59 in MM:SS format.")
        total = mins * 60 + sec
        if total <= 0:
            raise ValueError("Predicted time must be greater than 0 seconds.")
        return total
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        hrs, mins, sec = int(parts[0]), int(parts[1]), int(parts[2])
        if mins >= 60 or sec >= 60:
            raise ValueError("Minutes and seconds must be 00–59 in HH:MM:SS format.")
        total = hrs * 3600 + mins * 60 + sec
        if total <= 0:
            raise ValueError("Predicted time must be greater than 0 seconds.")
        return total
    raise ValueError("Invalid time format. Use MM:SS (e.g. 24:30) or HH:MM:SS.")


def format_mmss_int(seconds: int) -> str:
    """Format whole seconds as M:SS or H:MM:SS."""
    s = max(0, int(round(seconds)))
    hrs, rem = divmod(s, 3600)
    mins, sec = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{sec:02d}"
    return f"{mins}:{sec:02d}"


def format_spoken_time(seconds: int) -> str:
    """Natural spoken whole-second time (e.g. '24 minutes 30 seconds')."""
    s = max(0, int(round(seconds)))
    hrs, rem = divmod(s, 3600)
    mins, sec = divmod(rem, 60)
    parts: List[str] = []
    if hrs:
        parts.append(f"{hrs} {'hour' if hrs == 1 else 'hours'}")
    if mins:
        parts.append(f"{mins} {'minute' if mins == 1 else 'minutes'}")
    if sec or not parts:
        parts.append(f"{sec} {'second' if sec == 1 else 'seconds'}")
    return " ".join(parts)


class Predict5KManager:
    """SQLite-backed state & finish-line engine for Predict 5K events."""

    def __init__(
        self,
        db_path: Any,
        on_state_change: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_runner_finished: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._db_path_or_getter = db_path
        self._lock = threading.Lock()
        self._initialized_paths: set = set()
        self.on_state_change = on_state_change
        self.on_runner_finished = on_runner_finished
        self._ensure_schema()

    @property
    def db_path(self) -> str:
        return self._db_path_or_getter() if callable(self._db_path_or_getter) else str(self._db_path_or_getter)

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        if path not in self._initialized_paths:
            self._apply_schema(conn)
            self._initialized_paths.add(path)
        return conn

    def _ensure_schema(self) -> None:
        path = self.db_path
        with sqlite3.connect(path, check_same_thread=False, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            self._apply_schema(conn)
            self._initialized_paths.add(path)

    def _apply_schema(self, conn: sqlite3.Connection) -> None:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS predict_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT 'Club Predict 5K',
                    status TEXT NOT NULL DEFAULT 'setup',
                    countdown_seconds INTEGER NOT NULL DEFAULT 15,
                    min_finish_seconds INTEGER NOT NULL DEFAULT 600,
                    gun_time REAL DEFAULT NULL,
                    start_time REAL DEFAULT NULL,
                    revealed_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS predict_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES predict_sessions(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    predicted_seconds INTEGER NOT NULL,
                    epc TEXT DEFAULT NULL,
                    apriltag_id INTEGER DEFAULT NULL,
                    finish_time REAL DEFAULT NULL,
                    finish_source TEXT DEFAULT NULL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_predict_entries_epc
                    ON predict_entries(session_id, epc) WHERE epc IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_predict_entries_apriltag
                    ON predict_entries(session_id, apriltag_id) WHERE apriltag_id IS NOT NULL;
                """
            )
            if not conn.execute("SELECT 1 FROM predict_sessions LIMIT 1;").fetchone():
                conn.execute(
                    "INSERT INTO predict_sessions (name, status, created_at) VALUES (?, 'setup', ?);",
                    ("Club Predict 5K", clock.now()),
                )

    def _active_session_row(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM predict_sessions ORDER BY id DESC LIMIT 1;").fetchone()
        if not row:
            cur = conn.execute(
                "INSERT INTO predict_sessions (name, status, created_at) VALUES (?, 'setup', ?);",
                ("Club Predict 5K", clock.now()),
            )
            row = conn.execute("SELECT * FROM predict_sessions WHERE id = ?;", (cur.lastrowid,)).fetchone()
        sess = dict(row)
        # Auto-promote countdown -> running once gun_time is reached
        if sess["status"] == "countdown" and sess["gun_time"] and clock.now() >= sess["gun_time"]:
            conn.execute(
                "UPDATE predict_sessions SET status = 'running', start_time = gun_time WHERE id = ?;",
                (sess["id"],),
            )
            sess["status"] = "running"
            sess["start_time"] = sess["gun_time"]
        return sess

    @staticmethod
    def _clean_epc(epc: Optional[str]) -> Optional[str]:
        val = (epc or "").strip().upper()
        return val or None

    @staticmethod
    def _clean_apriltag(tag_id: Optional[int]) -> Optional[int]:
        if tag_id is None or tag_id == "":
            return None
        tid = int(tag_id)
        if not (0 <= tid <= 29):
            raise ValueError("AprilTag ID must be between 0 and 29.")
        return tid

    def add_entry(
        self,
        name: str,
        predicted_time: Any,
        epc: Optional[str] = None,
        apriltag_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Runner name is required.")
        pred_secs = parse_predicted_time(predicted_time)
        clean_epc = self._clean_epc(epc)
        clean_at = self._clean_apriltag(apriltag_id)
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            if clean_epc:
                dup = conn.execute(
                    "SELECT name FROM predict_entries WHERE session_id = ? AND epc = ?;",
                    (sess["id"], clean_epc),
                ).fetchone()
                if dup:
                    raise ValueError(f"RFID tag {clean_epc} is already assigned to {dup['name']}.")
            if clean_at is not None:
                dup = conn.execute(
                    "SELECT name FROM predict_entries WHERE session_id = ? AND apriltag_id = ?;",
                    (sess["id"], clean_at),
                ).fetchone()
                if dup:
                    raise ValueError(f"AprilTag #{clean_at} is already assigned to {dup['name']}.")
            cur = conn.execute(
                """INSERT INTO predict_entries
                   (session_id, name, predicted_seconds, epc, apriltag_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?);""",
                (sess["id"], clean_name, pred_secs, clean_epc, clean_at, clock.now()),
            )
            row = conn.execute("SELECT * FROM predict_entries WHERE id = ?;", (cur.lastrowid,)).fetchone()
            return dict(row)

    def delete_entry(self, entry_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM predict_entries WHERE id = ?;", (entry_id,))
            return cur.rowcount > 0

    def update_entry_prediction(self, entry_id: int, predicted_time: Any) -> Dict[str, Any]:
        """Update a runner's 5K prediction while the session is still in setup."""
        pred_secs = parse_predicted_time(predicted_time)
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            if sess["status"] != "setup":
                raise ValueError("Predictions are locked once the race has started.")
            row = conn.execute(
                "SELECT id, name FROM predict_entries WHERE id = ? AND session_id = ?;",
                (entry_id, sess["id"]),
            ).fetchone()
            if not row:
                raise ValueError("Runner entry not found.")
            conn.execute(
                "UPDATE predict_entries SET predicted_seconds = ? WHERE id = ?;",
                (pred_secs, entry_id),
            )
            return {"id": row["id"], "name": row["name"]}

    def update_settings(
        self,
        name: Optional[str] = None,
        countdown_seconds: Optional[int] = None,
        min_finish_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            new_name = (name.strip() or sess["name"]) if name is not None else sess["name"]
            new_cd = max(0, min(120, int(countdown_seconds))) if countdown_seconds is not None else sess["countdown_seconds"]
            new_min = max(1, min(7200, int(min_finish_seconds))) if min_finish_seconds is not None else sess["min_finish_seconds"]
            conn.execute(
                "UPDATE predict_sessions SET name = ?, countdown_seconds = ?, min_finish_seconds = ? WHERE id = ?;",
                (new_name, new_cd, new_min, sess["id"]),
            )
        return self.get_state()

    def start_race(self, countdown_override: Optional[int] = None) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            cd = sess["countdown_seconds"] if countdown_override is None else max(0, min(120, int(countdown_override)))
            now = clock.now()
            conn.execute(
                "UPDATE predict_entries SET finish_time = NULL, finish_source = NULL WHERE session_id = ?;",
                (sess["id"],),
            )
            if cd > 0:
                gun = now + cd
                conn.execute(
                    "UPDATE predict_sessions SET status = 'countdown', countdown_seconds = ?, gun_time = ?, start_time = NULL, revealed_count = 0 WHERE id = ?;",
                    (cd, gun, sess["id"]),
                )
            else:
                conn.execute(
                    "UPDATE predict_sessions SET status = 'running', countdown_seconds = 0, gun_time = ?, start_time = ?, revealed_count = 0 WHERE id = ?;",
                    (now, now, sess["id"]),
                )
        return self.get_state()

    def reset_race(self) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            conn.execute(
                "UPDATE predict_entries SET finish_time = NULL, finish_source = NULL WHERE session_id = ?;",
                (sess["id"],),
            )
            conn.execute(
                "UPDATE predict_sessions SET status = 'setup', gun_time = NULL, start_time = NULL, revealed_count = 0 WHERE id = ?;",
                (sess["id"],),
            )
        return self.get_state()

    def new_session(self, name: str = "Club Predict 5K") -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            conn.execute(
                """INSERT INTO predict_sessions
                   (name, status, countdown_seconds, min_finish_seconds, created_at)
                   VALUES (?, 'setup', ?, ?, ?);""",
                (name.strip() or "Club Predict 5K", sess["countdown_seconds"], sess["min_finish_seconds"], clock.now()),
            )
        return self.get_state()

    def record_finish(
        self, entry_id: int, finish_time: Optional[float] = None, source: str = "manual"
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            if sess["status"] != "running" or not sess["start_time"]:
                return None
            t = finish_time if finish_time is not None else clock.now()
            if (t - sess["start_time"]) < sess["min_finish_seconds"]:
                return None
            row = conn.execute(
                "SELECT * FROM predict_entries WHERE id = ? AND session_id = ?;",
                (entry_id, sess["id"]),
            ).fetchone()
            if not row or row["finish_time"] is not None:
                return None
            conn.execute(
                "UPDATE predict_entries SET finish_time = ?, finish_source = ? WHERE id = ?;",
                (t, source, entry_id),
            )
            return {
                "id": row["id"],
                "name": row["name"],
                "finished": True,
                "source": source,
            }

    def process_tag_read(self, tag_read: TagRead) -> Optional[Dict[str, Any]]:
        """Route RFID / AprilTag reads to the active Predict 5K session if running."""
        epc = (tag_read.epc or "").strip().upper()
        atid = getattr(tag_read, "apriltag_id", None)
        if atid is None:
            atid = epc_to_apriltag(epc)
        source = getattr(tag_read, "source", "rfid") or "rfid"

        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            if sess["status"] != "running" or not sess["start_time"]:
                return None
            if (tag_read.timestamp - sess["start_time"]) < sess["min_finish_seconds"]:
                return None

            row = None
            if atid is not None:
                row = conn.execute(
                    "SELECT * FROM predict_entries WHERE session_id = ? AND apriltag_id = ?;",
                    (sess["id"], atid),
                ).fetchone()
            if not row and epc:
                row = conn.execute(
                    "SELECT * FROM predict_entries WHERE session_id = ? AND epc = ?;",
                    (sess["id"], epc),
                ).fetchone()
            if not row or row["finish_time"] is not None:
                return None

            conn.execute(
                "UPDATE predict_entries SET finish_time = ?, finish_source = ? WHERE id = ?;",
                (tag_read.timestamp, source, row["id"]),
            )
            payload = {
                "id": row["id"],
                "name": row["name"],
                "finished": True,
                "source": source,
            }

        if self.on_runner_finished:
            self.on_runner_finished(payload)
        return payload

    def _compute_ranked_results(self, sess: Dict[str, Any], rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
        """Compute 1-second-accurate deltas & tie-aware ranks, ordered worst-to-first for reveal."""
        start_time = sess["start_time"] or 0.0
        scored: List[Dict[str, Any]] = []
        for r in rows:
            if r["finish_time"] is None:
                continue
            actual_secs = int(round(r["finish_time"] - start_time))
            pred_secs = int(r["predicted_seconds"])
            signed_delta = actual_secs - pred_secs
            abs_delta = abs(signed_delta)
            scored.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "predicted_seconds": pred_secs,
                    "predicted_formatted": format_mmss_int(pred_secs),
                    "actual_seconds": actual_secs,
                    "actual_formatted": format_mmss_int(actual_secs),
                    "signed_delta_seconds": signed_delta,
                    "abs_delta_seconds": abs_delta,
                    "source": r["finish_source"] or "manual",
                }
            )
        # Sort best-to-worst first to assign competition ranks (ties share rank)
        scored.sort(key=lambda x: (x["abs_delta_seconds"], x["actual_seconds"], x["name"].lower()))
        last_delta: Optional[int] = None
        current_rank = 0
        for idx, item in enumerate(scored):
            if item["abs_delta_seconds"] != last_delta:
                current_rank = idx + 1
                last_delta = item["abs_delta_seconds"]
            item["rank"] = current_rank
        winners = sum(1 for item in scored if item["rank"] == 1)
        for item in scored:
            item["is_winner"] = item["rank"] == 1
            item["is_tie"] = sum(1 for other in scored if other["rank"] == item["rank"]) > 1
            item["co_winners_count"] = winners if item["is_winner"] else 0

        # Reveal order: worst (highest abs_delta) first, winner(s) last
        scored.reverse()
        return scored

    def start_reveal(self) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            finishers = conn.execute(
                "SELECT COUNT(*) FROM predict_entries WHERE session_id = ? AND finish_time IS NOT NULL;",
                (sess["id"],),
            ).fetchone()[0]
            if finishers == 0:
                raise ValueError("No finishers recorded yet.")
            conn.execute(
                "UPDATE predict_sessions SET status = 'revealing', revealed_count = 0 WHERE id = ?;",
                (sess["id"],),
            )
        return self.get_state()

    def reveal_next(self) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            rows = conn.execute(
                "SELECT * FROM predict_entries WHERE session_id = ? ORDER BY id ASC;",
                (sess["id"],),
            ).fetchall()
            ranked_worst_first = self._compute_ranked_results(sess, rows)
            total = len(ranked_worst_first)
            if total == 0:
                raise ValueError("No finishers to reveal.")
            next_count = min(total, sess["revealed_count"] + 1)
            new_status = "ended" if next_count >= total else "revealing"
            conn.execute(
                "UPDATE predict_sessions SET status = ?, revealed_count = ? WHERE id = ?;",
                (new_status, next_count, sess["id"]),
            )
        return self.get_state()

    @staticmethod
    def _speech_for_entry(item: Dict[str, Any]) -> str:
        name = item["name"]
        pred = format_spoken_time(item["predicted_seconds"])
        actual = format_spoken_time(item["actual_seconds"])
        diff = item["abs_delta_seconds"]
        sd = item["signed_delta_seconds"]
        if diff == 0:
            delta_phrase = "Dead on! Zero seconds off!"
        else:
            unit = "second" if diff == 1 else "seconds"
            direction = "slower than predicted" if sd > 0 else "faster than predicted"
            delta_phrase = f"Off by {diff} {unit}, {diff} {unit} {direction}."
        if item["is_winner"]:
            if item["is_tie"]:
                return f"Co-winner! {name}! Predicted {pred}, actual {actual}. {delta_phrase}"
            return f"And our winner! {name}! Predicted {pred}, actual {actual}. {delta_phrase}"
        return f"Rank {item['rank']}! {name}. Predicted {pred}, actual {actual}. {delta_phrase}"

    def get_state(self) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            sess = self._active_session_row(conn)
            rows = conn.execute(
                "SELECT * FROM predict_entries WHERE session_id = ? ORDER BY id ASC;",
                (sess["id"],),
            ).fetchall()

        reveal_active = sess["status"] in ("revealing", "ended")
        ranked_worst_first = self._compute_ranked_results(sess, rows) if reveal_active else []
        revealed_count = min(sess["revealed_count"], len(ranked_worst_first)) if reveal_active else 0

        # Revealed list in reverse reveal order: newest revealed (better rank) at TOP of board!
        revealed_slice = ranked_worst_first[:revealed_count]
        for item in revealed_slice:
            item["speech"] = self._speech_for_entry(item)
        revealed_board = list(reversed(revealed_slice))
        latest_revealed = revealed_slice[-1] if revealed_slice else None

        entries_public: List[Dict[str, Any]] = []
        for r in rows:
            finished = r["finish_time"] is not None
            entry_out: Dict[str, Any] = {
                "id": r["id"],
                "name": r["name"],
                "epc": r["epc"],
                "apriltag_id": r["apriltag_id"],
                "finished": finished,
                "source": r["finish_source"],
                # Never expose predictions in the roster; only reveal on the Results Board
                "predicted_formatted": "Locked",
            }
            entries_public.append(entry_out)

        return {
            "session": {
                "id": sess["id"],
                "name": sess["name"],
                "status": sess["status"],
                "countdown_seconds": sess["countdown_seconds"],
                "min_finish_seconds": sess["min_finish_seconds"],
                "gun_time": sess["gun_time"],
                "start_time": sess["start_time"],
                "revealed_count": revealed_count,
                "total_finishers": sum(1 for r in rows if r["finish_time"] is not None),
                "total_entries": len(rows),
            },
            "server_now": clock.now(),
            "entries": entries_public,
            "revealed_board": revealed_board,
            "latest_revealed": latest_revealed,
        }
