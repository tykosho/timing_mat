import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager

from typing import Optional, List, Dict, Any, Tuple

from core import clock

logger = logging.getLogger("Database")

EVENT_STATUSES = ("setup", "running", "paused", "ended")
DEFAULT_EVENT_NAME = "Take Back the Track!"
_HEX_RE = re.compile(r"^[0-9A-F]+$")


class RegistrationError(ValueError):
    """A participant/tag change was rejected (duplicate bib, tag already in use, bad EPC...)."""


class EventStateError(ValueError):
    """An event lifecycle change isn't allowed from the current status."""


class LapEditError(ValueError):
    """A manual lap edit was rejected (runner not started, lap would end in the future, ...)."""


def normalize_epc(epc: Optional[str]) -> Optional[str]:
    """Upper-cases and validates an EPC. Blank means 'no tag assigned yet' (None)."""
    value = (epc or "").strip().upper()
    if not value:
        return None
    if len(value) % 2 or not _HEX_RE.match(value):
        raise RegistrationError(f"Tag EPC must be an even number of hex digits (got '{value}')")
    return value


class Database:
    """
    SQLite persistence. Data is organised per event: every participant, lap, break and raw
    tag read belongs to an event. Methods take an optional `event_id` and default to the
    *current* event (the one the reader is timing).
    """

    def __init__(self, db_path: str = "data/race.db", default_event_name: str = DEFAULT_EVENT_NAME):
        self.db_path = db_path
        self.default_event_name = default_event_name
        self._current_event_id: Optional[int] = None
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self):
        """Opens a connection, commits on success, rolls back on error, always closes."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        # With WAL (set once in init_db), NORMAL can lose at most the last commits on power
        # loss but never corrupts the database; FULL would fsync on every lap (issue #15).
        conn.execute("PRAGMA synchronous = NORMAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ schema
    def init_db(self):
        with self.connect() as conn:
            # Write-ahead log: a power cut mid-write (USB battery browning out) can't corrupt
            # the database, and readers don't block the lap engine's writes. Stored in the
            # file, so this sticks; backup() uses SQLite's backup API, which includes the WAL.
            mode = conn.execute("PRAGMA journal_mode = WAL;").fetchone()[0]
            if mode.lower() != "wal":
                logger.warning(f"Could not enable WAL on {self.db_path} (journal_mode={mode})")
            legacy = self._is_legacy_schema(conn)
        if legacy:
            # Single-event databases from before events existed: keep a copy, then migrate.
            self.backup("pre-events-migration")

        with self.connect() as conn:
            if legacy:
                for table in ("participants", "laps", "breaks"):
                    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy;")

            conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'setup',
                start_time REAL,
                end_time REAL,
                min_lap_seconds REAL DEFAULT 45.0,
                max_lap_rest_seconds REAL DEFAULT 600.0,
                track_length_m REAL DEFAULT 400.0,
                created_at REAL NOT NULL,
                paused_at REAL DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS participants (
                event_id INTEGER NOT NULL,
                bib_number INTEGER NOT NULL,
                name TEXT NOT NULL,
                email TEXT DEFAULT '',
                epc TEXT,
                notes TEXT DEFAULT '',
                created_at REAL NOT NULL,
                start_time REAL DEFAULT NULL,
                last_crossing_time REAL DEFAULT NULL,
                PRIMARY KEY (event_id, bib_number)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_epc
                ON participants(event_id, epc) WHERE epc IS NOT NULL;

            CREATE TABLE IF NOT EXISTS laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                bib_number INTEGER NOT NULL,
                lap_number INTEGER NOT NULL,
                crossing_time REAL NOT NULL,
                lap_duration_seconds REAL NOT NULL,
                peak_rssi REAL DEFAULT 0.0,
                manual_entry INTEGER DEFAULT 0,
                flag_reason TEXT DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_laps_event_bib ON laps(event_id, bib_number);

            CREATE TABLE IF NOT EXISTS breaks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                bib_number INTEGER NOT NULL,
                break_number INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                duration_seconds REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_breaks_event_bib ON breaks(event_id, bib_number);

            -- Every tag read as it came off the reader, so laps can be audited or re-derived later.
            CREATE TABLE IF NOT EXISTS raw_reads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                epc TEXT NOT NULL,
                timestamp REAL NOT NULL,
                rssi REAL DEFAULT 0.0
            );
            CREATE INDEX IF NOT EXISTS idx_raw_reads_event_time ON raw_reads(event_id, timestamp);
            """)

            if legacy:
                self._migrate_legacy_data(conn)

            # Columns added after the tables first shipped
            for table, column, ddl in [
                ("events", "paused_at", "REAL DEFAULT NULL"),      # issue #11
                ("laps", "flag_reason", "TEXT DEFAULT NULL"),      # suspicious-lap review (#10)
                ("participants", "start_flag_reason", "TEXT DEFAULT NULL"),  # missed start (#72)
            ]:
                cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table});")]
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")

            if not conn.execute("SELECT 1 FROM events LIMIT 1;").fetchone():
                self._insert_event(conn, self.default_event_name)

            row = conn.execute("SELECT value FROM settings WHERE key = 'current_event_id';").fetchone()
            current = int(row["value"]) if row else None
            if current is None or not conn.execute("SELECT 1 FROM events WHERE id = ?;", (current,)).fetchone():
                current = conn.execute("SELECT id FROM events ORDER BY id DESC LIMIT 1;").fetchone()["id"]
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_event_id', ?);", (str(current),))
            self._current_event_id = current

    @staticmethod
    def _is_legacy_schema(conn: sqlite3.Connection) -> bool:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(participants);")]
        return bool(cols) and "event_id" not in cols

    def _migrate_legacy_data(self, conn: sqlite3.Connection):
        """Moves a single-event database into event #1 (participants/laps/breaks gain an event_id)."""
        def legacy_cols(table: str) -> List[str]:
            return [r["name"] for r in conn.execute(f"PRAGMA table_info({table});")]

        state = None
        if legacy_cols("event_state"):
            state = conn.execute("SELECT * FROM event_state WHERE id = 1;").fetchone()
        state = dict(state) if state else {}

        if state.get("is_active"):
            status = "running"
        elif state.get("start_time"):
            status = "paused"
        else:
            status = "setup"
        event_id = self._insert_event(
            conn, state.get("name") or self.default_event_name, status=status,
            start_time=state.get("start_time"),
            min_lap_seconds=state.get("min_lap_seconds") or 45.0,
            max_lap_rest_seconds=state.get("max_lap_rest_seconds") or 600.0,
        )

        def col(table_cols: List[str], name: str, default: str = "NULL") -> str:
            return name if name in table_cols else default

        pc = legacy_cols("participants_legacy")
        conn.execute(f"""
            INSERT INTO participants (event_id, bib_number, name, email, epc, notes, created_at, start_time, last_crossing_time)
            SELECT ?, bib_number, name, '', epc, {col(pc, 'notes', "''")}, {col(pc, 'created_at', '0')},
                   {col(pc, 'start_time')}, {col(pc, 'last_crossing_time')}
            FROM participants_legacy;""", (event_id,))
        lc = legacy_cols("laps_legacy")
        conn.execute(f"""
            INSERT INTO laps (id, event_id, bib_number, lap_number, crossing_time, lap_duration_seconds, peak_rssi, manual_entry)
            SELECT id, ?, bib_number, lap_number, crossing_time, lap_duration_seconds,
                   {col(lc, 'peak_rssi', '0.0')}, {col(lc, 'manual_entry', '0')}
            FROM laps_legacy;""", (event_id,))
        conn.execute("""
            INSERT INTO breaks (id, event_id, bib_number, break_number, start_time, end_time, duration_seconds)
            SELECT id, ?, bib_number, break_number, start_time, end_time, duration_seconds FROM breaks_legacy;""",
            (event_id,))

        for table in ("participants_legacy", "laps_legacy", "breaks_legacy"):
            conn.execute(f"DROP TABLE {table};")
        if legacy_cols("event_state"):
            conn.execute("DROP TABLE event_state;")

    def _insert_event(self, conn: sqlite3.Connection, name: str, status: str = "setup",
                      start_time: Optional[float] = None, min_lap_seconds: float = 45.0,
                      max_lap_rest_seconds: float = 600.0, track_length_m: float = 400.0) -> int:
        cursor = conn.execute("""
            INSERT INTO events (name, status, start_time, min_lap_seconds, max_lap_rest_seconds, track_length_m, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);""",
            (name.strip() or self.default_event_name, status, start_time, min_lap_seconds,
             max_lap_rest_seconds, track_length_m, clock.now()))
        return cursor.lastrowid

    def backup(self, label: str = "manual") -> str:
        """Writes a consistent copy of the database to <data dir>/backups/ and returns its path."""
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(self.db_path)), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(self.db_path))[0]
        dest_path = os.path.join(backup_dir, f"{base}-{label}-{time.strftime('%Y%m%d-%H%M%S')}.db")
        src = sqlite3.connect(self.db_path)
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        return dest_path

    # ------------------------------------------------------------------ events
    @property
    def current_event_id(self) -> int:
        return self._current_event_id

    def _eid(self, event_id: Optional[int]) -> int:
        return self._current_event_id if event_id is None else event_id

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> Dict[str, Any]:
        event = dict(row)
        event["is_active"] = 1 if event["status"] == "running" else 0
        event["is_ended"] = 1 if event["status"] == "ended" else 0
        return event

    def get_event(self, event_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?;", (self._eid(event_id),)).fetchone()
            return self._event_dict(row) if row else None

    def get_event_state(self) -> Dict[str, Any]:
        """The current event (kept under its old name; the engine and pages call this)."""
        return self.get_event()

    def list_events(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT e.*,
                  (SELECT COUNT(*) FROM participants p WHERE p.event_id = e.id) AS participant_count,
                  (SELECT COUNT(*) FROM participants p WHERE p.event_id = e.id AND p.epc IS NOT NULL) AS tagged_count,
                  (SELECT COUNT(*) FROM laps l WHERE l.event_id = e.id) AS lap_count
                FROM events e ORDER BY e.id DESC;""").fetchall()
            events = []
            for r in rows:
                ev = self._event_dict(r)
                ev["is_current"] = ev["id"] == self._current_event_id
                events.append(ev)
            return events

    def create_event(self, name: str) -> int:
        """Creates an event in 'setup', inheriting rules (lap debounce, rest, track length) from the current one."""
        base = self.get_event() or {}
        max_rest = base.get("max_lap_rest_seconds")
        if max_rest is None:
            max_rest = 600.0
        with self.connect() as conn:
            return self._insert_event(
                conn, name, min_lap_seconds=base.get("min_lap_seconds", 45.0),
                max_lap_rest_seconds=max_rest,
                track_length_m=base.get("track_length_m", 400.0))

    def set_current_event(self, event_id: int):
        """Switches which event the reader is timing. Not allowed while the current one is running."""
        if event_id == self._current_event_id:
            return
        current = self.get_event()
        if current and current["status"] == "running":
            raise EventStateError("Pause or end the running event before switching events.")
        if not self.get_event(event_id):
            raise EventStateError(f"Event {event_id} does not exist.")
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_event_id', ?);", (str(event_id),))
        self._current_event_id = event_id

    def update_event_settings(self, name: Optional[str] = None, min_lap_seconds: Optional[float] = None,
                              max_lap_rest_seconds: Optional[float] = None,
                              track_length_m: Optional[float] = None, event_id: Optional[int] = None):
        ev = self.get_event(event_id)
        with self.connect() as conn:
            conn.execute("""
                UPDATE events SET name = ?, min_lap_seconds = ?, max_lap_rest_seconds = ?, track_length_m = ?
                WHERE id = ?;""", (
                (name.strip() if name and name.strip() else ev["name"]),
                ev["min_lap_seconds"] if min_lap_seconds is None else min_lap_seconds,
                ev["max_lap_rest_seconds"] if max_lap_rest_seconds is None else max_lap_rest_seconds,
                ev["track_length_m"] if track_length_m is None else track_length_m,
                ev["id"]))

    def _set_status(self, ev: Dict[str, Any], status: str, start_time: Optional[float] = None,
                    end_time: Optional[float] = None, paused_at: Optional[float] = None):
        with self.connect() as conn:
            conn.execute("UPDATE events SET status = ?, start_time = ?, end_time = ?, paused_at = ? WHERE id = ?;",
                         (status, start_time, end_time, paused_at, ev["id"]))

    def start_event(self, at: Optional[float] = None) -> Dict[str, Any]:
        """setup/paused -> running. The race clock keeps its original start across pauses."""
        ev = self.get_event()
        if ev["status"] == "ended":
            raise EventStateError("This event has ended. Reopen it to continue timing.")
        if ev["status"] != "running":
            now = at or clock.now()
            if ev["status"] == "paused" and ev.get("paused_at"):
                self._skip_pause(ev["id"], max(0.0, now - ev["paused_at"]))
            self._set_status(ev, "running", ev["start_time"] or now, None)
        return self.get_event()

    def _skip_pause(self, event_id: int, pause_seconds: float):
        """Moves every started runner's last crossing forward by the pause (issue #11).

        Crossings aren't timed while paused, so without this the first lap after resuming
        would include the whole pause. Open manual breaks are left alone.
        """
        if pause_seconds <= 0:
            return
        with self.connect() as conn:
            conn.execute("""UPDATE participants SET last_crossing_time = last_crossing_time + ?
                            WHERE event_id = ? AND last_crossing_time IS NOT NULL;""",
                         (pause_seconds, event_id))

    def pause_event(self) -> Dict[str, Any]:
        ev = self.get_event()
        if ev["status"] == "ended":
            raise EventStateError("This event has already ended.")
        if ev["status"] == "running":
            self._set_status(ev, "paused", ev["start_time"], None, paused_at=clock.now())
        return self.get_event()

    def end_event(self, at: Optional[float] = None) -> Dict[str, Any]:
        """running/paused -> ended. Freezes results; any runner still on a manual break is closed out."""
        ev = self.get_event()
        if ev["status"] == "setup":
            raise EventStateError("The event hasn't started yet.")
        if ev["status"] == "ended":
            return ev
        now = at or clock.now()
        with self.connect() as conn:
            for b in conn.execute("SELECT id, start_time FROM breaks WHERE event_id = ? AND end_time = 0.0;", (ev["id"],)).fetchall():
                conn.execute("UPDATE breaks SET end_time = ?, duration_seconds = ? WHERE id = ?;",
                             (now, round(max(0.0, now - b["start_time"]), 2), b["id"]))
        self._set_status(ev, "ended", ev["start_time"], now)
        return self.get_event()

    def reopen_event(self) -> Dict[str, Any]:
        """ended -> paused, for an End pressed by mistake."""
        ev = self.get_event()
        if ev["status"] == "ended":
            # Timing stopped at end_time, so the "pause" runs from then until Start
            self._set_status(ev, "paused", ev["start_time"], None, paused_at=ev["end_time"])
        return self.get_event()

    # ------------------------------------------------------------------ participants
    def _find_tag_owner(self, conn: sqlite3.Connection, event_id: int, epc: str,
                        exclude_bib: Optional[int] = None) -> Optional[sqlite3.Row]:
        row = conn.execute("SELECT bib_number, name FROM participants WHERE event_id = ? AND epc = ?;", (event_id, epc)).fetchone()
        if row and row["bib_number"] != exclude_bib:
            return row
        return None

    def add_participant(self, bib_number: int, name: str, epc: Optional[str] = None, notes: str = "",
                        email: str = "", event_id: Optional[int] = None) -> bool:
        eid = self._eid(event_id)
        epc = normalize_epc(epc)
        name = (name or "").strip()
        if not name:
            raise RegistrationError("Name is required")
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM participants WHERE event_id = ? AND bib_number = ?;", (eid, bib_number)).fetchone():
                raise RegistrationError(f"Bib #{bib_number} is already registered")
            if epc:
                owner = self._find_tag_owner(conn, eid, epc)
                if owner:
                    raise RegistrationError(f"Tag {epc} is already assigned to #{owner['bib_number']} {owner['name']}")
            conn.execute("""
                INSERT INTO participants (event_id, bib_number, name, email, epc, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);""",
                (eid, bib_number, name, (email or "").strip(), epc, (notes or "").strip(), clock.now()))
        return True

    def update_participant(self, bib_number: int, name: str, epc: Optional[str] = None, notes: str = "",
                           email: Optional[str] = None, event_id: Optional[int] = None) -> bool:
        eid = self._eid(event_id)
        epc = normalize_epc(epc)
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM participants WHERE event_id = ? AND bib_number = ?;", (eid, bib_number)).fetchone()
            if not current:
                raise RegistrationError(f"Bib #{bib_number} is not registered")
            if epc:
                owner = self._find_tag_owner(conn, eid, epc, exclude_bib=bib_number)
                if owner:
                    raise RegistrationError(f"Tag {epc} is already assigned to #{owner['bib_number']} {owner['name']}")
            conn.execute("""
                UPDATE participants SET name = ?, epc = ?, notes = ?, email = ?
                WHERE event_id = ? AND bib_number = ?;""",
                ((name or "").strip() or current["name"], epc, (notes or "").strip(),
                 current["email"] if email is None else email.strip(), eid, bib_number))
        return True

    def set_participant_tag(self, bib_number: int, epc: Optional[str], event_id: Optional[int] = None):
        """Assigns (or with epc=None, clears) the tag for a bib. Rejects a tag already used by another bib."""
        eid = self._eid(event_id)
        epc = normalize_epc(epc)
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM participants WHERE event_id = ? AND bib_number = ?;", (eid, bib_number)).fetchone():
                raise RegistrationError(f"Bib #{bib_number} is not registered")
            if epc:
                owner = self._find_tag_owner(conn, eid, epc, exclude_bib=bib_number)
                if owner:
                    raise RegistrationError(f"Tag {epc} is already assigned to #{owner['bib_number']} {owner['name']}")
            conn.execute("UPDATE participants SET epc = ? WHERE event_id = ? AND bib_number = ?;", (epc, eid, bib_number))

    def delete_participant(self, bib_number: int, event_id: Optional[int] = None) -> bool:
        eid = self._eid(event_id)
        with self.connect() as conn:
            for table in ("laps", "breaks", "participants"):
                conn.execute(f"DELETE FROM {table} WHERE event_id = ? AND bib_number = ?;", (eid, bib_number))
        return True

    def set_participant_start(self, bib_number: int, start_time: float, event_id: Optional[int] = None):
        with self.connect() as conn:
            conn.execute("UPDATE participants SET start_time = ?, last_crossing_time = ? WHERE event_id = ? AND bib_number = ?;",
                         (start_time, start_time, self._eid(event_id), bib_number))

    def update_participant_last_crossing(self, bib_number: int, crossing_time: float, event_id: Optional[int] = None):
        with self.connect() as conn:
            conn.execute("UPDATE participants SET last_crossing_time = ? WHERE event_id = ? AND bib_number = ?;",
                         (crossing_time, self._eid(event_id), bib_number))

    def reset_participant_start(self, bib_number: int, event_id: Optional[int] = None):
        eid = self._eid(event_id)
        with self.connect() as conn:
            conn.execute("""UPDATE participants SET start_time = NULL, last_crossing_time = NULL, start_flag_reason = NULL
                            WHERE event_id = ? AND bib_number = ?;""", (eid, bib_number))
            conn.execute("DELETE FROM laps WHERE event_id = ? AND bib_number = ?;", (eid, bib_number))
            conn.execute("DELETE FROM breaks WHERE event_id = ? AND bib_number = ?;", (eid, bib_number))

    def get_participant_by_epc(self, epc: str, event_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM participants WHERE event_id = ? AND epc = ?;",
                               (self._eid(event_id), (epc or "").strip().upper())).fetchone()
            return dict(row) if row else None

    # --- missed starts (issue #72) ---
    def flag_start(self, bib_number: int, reason: str, event_id: Optional[int] = None):
        with self.connect() as conn:
            conn.execute("UPDATE participants SET start_flag_reason = ? WHERE event_id = ? AND bib_number = ?;",
                         (reason, self._eid(event_id), bib_number))

    def get_flagged_starts(self, event_id: Optional[int] = None) -> List[Dict[str, Any]]:
        eid = self._eid(event_id)
        with self.connect() as conn:
            gun = conn.execute("SELECT start_time FROM events WHERE id = ?;", (eid,)).fetchone()["start_time"]
            rows = conn.execute("""SELECT bib_number, name, start_time, start_flag_reason FROM participants
                                   WHERE event_id = ? AND start_flag_reason IS NOT NULL AND start_time IS NOT NULL
                                   ORDER BY start_time DESC;""", (eid,)).fetchall()
        return [dict(r, seconds_after_gun=round(r["start_time"] - gun, 1) if gun else None) for r in rows]

    def typical_lap_seconds(self, min_laps: int = 5, event_id: Optional[int] = None) -> Optional[float]:
        """Median lap time of everyone so far, or None before there are min_laps laps."""
        with self.connect() as conn:
            rows = [r[0] for r in conn.execute(
                "SELECT lap_duration_seconds FROM laps WHERE event_id = ? ORDER BY lap_duration_seconds;",
                (self._eid(event_id),)).fetchall()]
        return rows[len(rows) // 2] if len(rows) >= min_laps else None

    def keep_start(self, bib_number: int, event_id: Optional[int] = None):
        """The runner really did join late: keep their start, drop the flag."""
        self.flag_start(bib_number, None, event_id)

    def start_from_gun(self, bib_number: int, event_id: Optional[int] = None) -> Dict[str, Any]:
        """The runner's tag wasn't read at the gun, so their first lap's crossing became their
        start. Moves their start to the gun and turns that crossing into lap 1."""
        eid = self._eid(event_id)
        with self.connect() as conn:
            gun_row = conn.execute("SELECT start_time, min_lap_seconds FROM events WHERE id = ?;", (eid,)).fetchone()
            gun = gun_row["start_time"] if gun_row else None
            min_lap = (gun_row["min_lap_seconds"] if gun_row else None) or 0.0
            p = conn.execute("SELECT * FROM participants WHERE event_id = ? AND bib_number = ?;",
                             (eid, bib_number)).fetchone()
            if not p or p["start_time"] is None:
                raise LapEditError(f"Bib {bib_number} hasn't started.")
            if not gun or p["start_time"] - gun < min_lap:
                raise LapEditError(f"Bib {bib_number} started less than a lap after the gun: "
                                   "there's no missed lap to add.")
            first_seen = p["start_time"]
            conn.execute("""INSERT INTO laps (event_id, bib_number, lap_number, crossing_time,
                                              lap_duration_seconds, peak_rssi, manual_entry, flag_reason)
                            VALUES (?, ?, 0, ?, ?, 0.0, 1, NULL);""",
                         (eid, bib_number, first_seen, round(first_seen - gun, 2)))
            conn.execute("""UPDATE participants SET start_time = ?, start_flag_reason = NULL,
                            last_crossing_time = MAX(COALESCE(last_crossing_time, ?), ?)
                            WHERE event_id = ? AND bib_number = ?;""",
                         (gun, first_seen, first_seen, eid, bib_number))
            self._renumber_laps(conn, eid, bib_number)
        return {"bib_number": bib_number, "start_time": gun, "lap_seconds": round(first_seen - gun, 2)}

    def get_participant_by_bib(self, bib_number: int, event_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM participants WHERE event_id = ? AND bib_number = ?;",
                               (self._eid(event_id), bib_number)).fetchone()
            return dict(row) if row else None

    def get_all_participants(self, event_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM participants WHERE event_id = ? ORDER BY bib_number ASC;",
                                (self._eid(event_id),)).fetchall()
            return [dict(r) for r in rows]

    def bulk_add_registrations(self, rows: List[Dict[str, Any]], first_bib: int = 1,
                               event_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Adds parsed registrations (dicts with name, email, notes and optionally bib / epc).
        Rows without a bib get the lowest free number >= first_bib. Duplicates (same email, or
        same name when no email) and conflicting bibs/tags are skipped and reported, never overwritten.
        """
        eid = self._eid(event_id)
        imported: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        with self.connect() as conn:
            existing = conn.execute("SELECT bib_number, name, email, epc FROM participants WHERE event_id = ?;", (eid,)).fetchall()
            used_bibs = {r["bib_number"] for r in existing}
            used_epcs = {r["epc"] for r in existing if r["epc"]}
            seen_emails = {r["email"].lower() for r in existing if r["email"]}
            seen_names = {r["name"].lower() for r in existing if not r["email"]}
            # Keep explicit bibs from the file out of the auto-assignment pool
            reserved = {r["bib"] for r in rows if r.get("bib") is not None}
            next_bib = first_bib

            for r in rows:
                name, email = r["name"].strip(), (r.get("email") or "").strip()
                label = {"line": r.get("line"), "name": name}
                if email and email.lower() in seen_emails:
                    skipped.append({**label, "reason": f"already registered (same email {email})"})
                    continue
                if not email and name.lower() in seen_names:
                    skipped.append({**label, "reason": "already registered (same name)"})
                    continue

                bib = r.get("bib")
                if bib is not None:
                    if bib in used_bibs:
                        skipped.append({**label, "reason": f"bib #{bib} already taken"})
                        continue
                else:
                    while next_bib in used_bibs or next_bib in reserved:
                        next_bib += 1
                    bib = next_bib

                epc = r.get("epc")
                if epc and epc in used_epcs:
                    skipped.append({**label, "reason": f"tag {epc} already assigned"})
                    continue

                conn.execute("""
                    INSERT INTO participants (event_id, bib_number, name, email, epc, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?);""",
                    (eid, bib, name, email, epc, (r.get("notes") or "").strip(), clock.now()))
                used_bibs.add(bib)
                if epc:
                    used_epcs.add(epc)
                if email:
                    seen_emails.add(email.lower())
                else:
                    seen_names.add(name.lower())
                imported.append({**label, "bib": bib})
        return {"imported": imported, "skipped": skipped}

    # ------------------------------------------------------------------ laps
    def record_lap(self, bib_number: int, lap_number: int, crossing_time: float,
                   lap_duration_seconds: float, peak_rssi: float = 0.0, manual_entry: bool = False,
                   flag_reason: Optional[str] = None, event_id: Optional[int] = None) -> int:
        with self.connect() as conn:
            cursor = conn.execute("""
                INSERT INTO laps (event_id, bib_number, lap_number, crossing_time, lap_duration_seconds, peak_rssi, manual_entry, flag_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);""",
                (self._eid(event_id), bib_number, lap_number, crossing_time, lap_duration_seconds,
                 peak_rssi, 1 if manual_entry else 0, flag_reason))
            return cursor.lastrowid

    def get_flagged_laps(self, event_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Returns laps that were flagged as suspicious (e.g. possible missed lap)."""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT l.*, p.name FROM laps l
                LEFT JOIN participants p ON p.event_id = l.event_id AND p.bib_number = l.bib_number
                WHERE l.event_id = ? AND l.flag_reason IS NOT NULL
                ORDER BY l.crossing_time DESC;""", (self._eid(event_id),)).fetchall()
            return [dict(r) for r in rows]

    def clear_lap_flag(self, lap_id: int):
        """Clears the review flag for a lap (accepts the split)."""
        with self.connect() as conn:
            conn.execute("UPDATE laps SET flag_reason = NULL WHERE id = ?;", (lap_id,))

    def split_lap(self, lap_id: int, event_id: Optional[int] = None) -> bool:
        """Splits a flagged double lap into 2 equal-time laps."""
        eid = self._eid(event_id)
        with self.connect() as conn:
            lap = conn.execute("SELECT * FROM laps WHERE id = ?;", (lap_id,)).fetchone()
            if not lap:
                return False
            lap = dict(lap)
            bib = lap["bib_number"]
            total_dur = lap["lap_duration_seconds"]
            half_dur = round(total_dur / 2.0, 2)
            orig_crossing = lap["crossing_time"]
            mid_crossing = round(orig_crossing - half_dur, 2)
            lap_num = lap["lap_number"]

            # The split lap keeps its crossing and gets the second half...
            conn.execute("""
                UPDATE laps
                SET lap_duration_seconds = ?, flag_reason = NULL
                WHERE id = ?;
            """, (half_dur, lap_id))

            # ...and an interpolated crossing is inserted halfway through it
            conn.execute("""
                INSERT INTO laps (event_id, bib_number, lap_number, crossing_time, lap_duration_seconds, peak_rssi, manual_entry, flag_reason)
                VALUES (?, ?, ?, ?, ?, ?, 1, NULL);
            """, (eid, bib, lap_num, mid_crossing, half_dur, lap["peak_rssi"]))

            self._renumber_laps(conn, eid, bib)
            return True

    def get_last_lap(self, bib_number: int, event_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("""
                SELECT * FROM laps WHERE event_id = ? AND bib_number = ?
                ORDER BY lap_number DESC LIMIT 1;""", (self._eid(event_id), bib_number)).fetchone()
            return dict(row) if row else None

    def get_laps_for_runner(self, bib_number: int, event_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM laps WHERE event_id = ? AND bib_number = ? ORDER BY lap_number ASC;",
                                (self._eid(event_id), bib_number)).fetchall()
            return [dict(r) for r in rows]

    def get_all_laps(self, event_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT l.*, p.name FROM laps l
                LEFT JOIN participants p ON p.event_id = l.event_id AND p.bib_number = l.bib_number
                WHERE l.event_id = ? ORDER BY l.crossing_time ASC;""", (self._eid(event_id),)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_crossings(self, limit: int = 10, event_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Returns the N most recent lap crossings in reverse chronological order."""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT l.id, l.bib_number, p.name, l.lap_number, l.crossing_time,
                       l.lap_duration_seconds, l.peak_rssi, l.manual_entry
                FROM laps l
                LEFT JOIN participants p ON p.event_id = l.event_id AND p.bib_number = l.bib_number
                WHERE l.event_id = ?
                ORDER BY l.crossing_time DESC, l.id DESC
                LIMIT ?;""", (self._eid(event_id), limit)).fetchall()
            return [dict(r) for r in rows]

    # Lap edits (issue #9). A lap's duration is the time since the runner's previous crossing
    # (adjusted for pauses and breaks), so removing or adding a lap must hand its time to
    # whatever follows, renumber, and keep last_crossing_time right. Each edit is one transaction.

    @staticmethod
    def _renumber_laps(conn: sqlite3.Connection, eid: int, bib_number: int):
        """Numbers a runner's laps 1..n in crossing order."""
        rows = conn.execute("""SELECT id FROM laps WHERE event_id = ? AND bib_number = ?
                               ORDER BY crossing_time, id;""", (eid, bib_number)).fetchall()
        for number, row in enumerate(rows, start=1):
            conn.execute("UPDATE laps SET lap_number = ? WHERE id = ?;", (number, row["id"]))

    def delete_lap(self, lap_id: int) -> Optional[Dict[str, Any]]:
        """Removes a lap (e.g. a ghost read) and gives its time to what came after it.

        - A later lap: it absorbs the deleted lap's time (two laps become one longer one).
        - A break that started at this crossing: the break absorbs it instead.
        - Nothing after it: the runner's current lap is treated as starting where the deleted
          lap started, so the next crossing is timed correctly.
        Returns the deleted lap, or None if it didn't exist.
        """
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM laps WHERE id = ?;", (lap_id,)).fetchone()
            if not row:
                return None
            lap = dict(row)
            eid, bib, crossing, duration = lap["event_id"], lap["bib_number"], lap["crossing_time"], lap["lap_duration_seconds"]

            next_lap = conn.execute("""
                SELECT id, crossing_time FROM laps
                WHERE event_id = ? AND bib_number = ? AND id != ? AND crossing_time >= ?
                ORDER BY crossing_time, id LIMIT 1;""", (eid, bib, lap_id, crossing)).fetchone()
            # An auto-detected break starts exactly at the crossing before it
            next_break = conn.execute("""
                SELECT id FROM breaks
                WHERE event_id = ? AND bib_number = ? AND ABS(start_time - ?) < 0.01
                LIMIT 1;""", (eid, bib, crossing)).fetchone()

            conn.execute("DELETE FROM laps WHERE id = ?;", (lap_id,))
            if next_break:
                conn.execute("""UPDATE breaks SET start_time = start_time - ?,
                                    duration_seconds = CASE WHEN end_time > 0 THEN duration_seconds + ? ELSE duration_seconds END
                                WHERE id = ?;""", (duration, duration, next_break["id"]))
            elif next_lap:
                conn.execute("UPDATE laps SET lap_duration_seconds = ROUND(lap_duration_seconds + ?, 2) WHERE id = ?;",
                             (duration, next_lap["id"]))
            else:
                conn.execute("""UPDATE participants SET last_crossing_time = last_crossing_time - ?
                                WHERE event_id = ? AND bib_number = ? AND last_crossing_time IS NOT NULL;""",
                             (duration, eid, bib))
            self._renumber_laps(conn, eid, bib)
            return lap

    def delete_last_lap_for_runner(self, bib_number: int, event_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Deletes the runner's most recent lap (see delete_lap)."""
        last = self.get_last_lap(bib_number, event_id)
        return self.delete_lap(last["id"]) if last else None

    def add_manual_lap(self, bib_number: int, duration: Optional[float] = None,
                       min_gap: float = 0.0, event_id: Optional[int] = None) -> Dict[str, Any]:
        """Records a lap the reader missed.

        With `duration`, the lap ends that long after the runner's previous crossing. Without
        it, the lap ends now and is timed from the previous crossing, which must be at least
        `min_gap` seconds ago (so a double tap doesn't add a second lap). Either way the next
        automatic lap is timed from this one. Never invents a time.
        """
        eid = self._eid(event_id)
        now = clock.now()
        with self.connect() as conn:
            p = conn.execute("SELECT * FROM participants WHERE event_id = ? AND bib_number = ?;",
                             (eid, bib_number)).fetchone()
            if not p:
                raise LapEditError(f"Runner #{bib_number} isn't registered in this event.")
            previous = p["last_crossing_time"] or p["start_time"]
            if previous is None:
                raise LapEditError(f"Runner #{bib_number} hasn't started yet, so there's no lap to time.")
            if duration is not None:
                if duration <= 0:
                    raise LapEditError("Lap duration must be positive.")
                crossing = previous + duration
                if crossing > now + 1.0:
                    raise LapEditError(f"Runner #{bib_number}'s current lap started {now - previous:.0f}s ago, "
                                       f"so a {duration:.0f}s lap would end in the future.")
            else:
                crossing, duration = now, now - previous
                if duration < min_gap:
                    raise LapEditError(f"Only {duration:.0f}s since runner #{bib_number}'s last crossing "
                                       f"(minimum lap is {min_gap:.0f}s); no lap added.")
            cursor = conn.execute("""
                INSERT INTO laps (event_id, bib_number, lap_number, crossing_time, lap_duration_seconds, peak_rssi, manual_entry)
                VALUES (?, ?, 0, ?, ?, 0.0, 1);""", (eid, bib_number, crossing, round(duration, 2)))
            conn.execute("UPDATE participants SET last_crossing_time = ? WHERE event_id = ? AND bib_number = ?;",
                         (crossing, eid, bib_number))
            self._renumber_laps(conn, eid, bib_number)
            lap = dict(conn.execute("SELECT * FROM laps WHERE id = ?;", (cursor.lastrowid,)).fetchone())
            lap["name"] = p["name"]
            return lap

    def get_total_laps_count(self, event_id: Optional[int] = None) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM laps WHERE event_id = ?;", (self._eid(event_id),)).fetchone()["c"]

    # ------------------------------------------------------------------ raw reads
    def insert_raw_reads(self, reads: List[Tuple[int, str, float, float]]):
        """reads: (event_id, epc, timestamp, rssi) tuples."""
        if not reads:
            return
        with self.connect() as conn:
            conn.executemany("INSERT INTO raw_reads (event_id, epc, timestamp, rssi) VALUES (?, ?, ?, ?);", reads)

    def get_raw_reads(self, event_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM raw_reads WHERE event_id = ? ORDER BY timestamp ASC, id ASC;",
                                (self._eid(event_id),)).fetchall()
            return [dict(r) for r in rows]

    def count_raw_reads(self, event_id: Optional[int] = None) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM raw_reads WHERE event_id = ?;", (self._eid(event_id),)).fetchone()["c"]

    # ------------------------------------------------------------------ clock support
    def latest_timestamp(self) -> Optional[float]:
        """The newest time stored anywhere, so the race clock never starts before it (issue #2)."""
        with self.connect() as conn:
            row = conn.execute("""
                SELECT MAX(t) AS t FROM (
                    SELECT MAX(MAX(COALESCE(start_time, 0), COALESCE(end_time, 0), created_at)) AS t FROM events
                    UNION ALL SELECT MAX(MAX(COALESCE(start_time, 0), COALESCE(last_crossing_time, 0), created_at)) FROM participants
                    UNION ALL SELECT MAX(crossing_time) FROM laps
                    UNION ALL SELECT MAX(end_time) FROM breaks
                    UNION ALL SELECT MAX(timestamp) FROM raw_reads
                );""").fetchone()
            return row["t"] if row and row["t"] else None

    def race_in_progress(self) -> bool:
        """True while any event is running or paused, i.e. timestamps are being compared."""
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM events WHERE status IN ('running', 'paused') LIMIT 1;").fetchone()
            return row is not None

    # ------------------------------------------------------------------ breaks
    def record_break(self, bib_number: int, start_time: float, end_time: float, duration_seconds: float,
                     event_id: Optional[int] = None) -> int:
        eid = self._eid(event_id)
        with self.connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM breaks WHERE event_id = ? AND bib_number = ?;", (eid, bib_number)).fetchone()["c"]
            cursor = conn.execute("""
                INSERT INTO breaks (event_id, bib_number, break_number, start_time, end_time, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?);""", (eid, bib_number, count + 1, start_time, end_time, duration_seconds))
            return cursor.lastrowid

    def get_breaks_for_runner(self, bib_number: int, event_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM breaks WHERE event_id = ? AND bib_number = ? ORDER BY break_number ASC;",
                                (self._eid(event_id), bib_number)).fetchall()
            return [dict(r) for r in rows]

    def get_open_break(self, bib_number: int, event_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("""
                SELECT * FROM breaks WHERE event_id = ? AND bib_number = ? AND end_time = 0.0
                ORDER BY id DESC LIMIT 1;""", (self._eid(event_id), bib_number)).fetchone()
            return dict(row) if row else None

    def start_manual_break(self, bib_number: int, event_id: Optional[int] = None) -> Dict[str, Any]:
        eid, now = self._eid(event_id), clock.now()
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM breaks WHERE event_id = ? AND bib_number = ? AND end_time = 0.0;", (eid, bib_number)).fetchone():
                count = conn.execute("SELECT COUNT(*) AS c FROM breaks WHERE event_id = ? AND bib_number = ?;", (eid, bib_number)).fetchone()["c"]
                conn.execute("""
                    INSERT INTO breaks (event_id, bib_number, break_number, start_time, end_time, duration_seconds)
                    VALUES (?, ?, ?, ?, 0.0, 0.0);""", (eid, bib_number, count + 1, now))
        return {"status": "on_break", "bib": bib_number, "start_time": now}

    def end_manual_break(self, bib_number: int, event_id: Optional[int] = None) -> Dict[str, Any]:
        eid, now = self._eid(event_id), clock.now()
        duration = 0.0
        with self.connect() as conn:
            open_b = conn.execute("SELECT * FROM breaks WHERE event_id = ? AND bib_number = ? AND end_time = 0.0 ORDER BY id DESC LIMIT 1;",
                                  (eid, bib_number)).fetchone()
            if open_b:
                duration = max(0.0, now - open_b["start_time"])
                conn.execute("UPDATE breaks SET end_time = ?, duration_seconds = ? WHERE id = ?;",
                             (now, round(duration, 2), open_b["id"]))
            conn.execute("UPDATE participants SET last_crossing_time = ? WHERE event_id = ? AND bib_number = ?;", (now, eid, bib_number))
        return {"status": "resumed", "bib": bib_number, "duration_seconds": round(duration, 2)}

    # ------------------------------------------------------------------ leaderboard
    def get_leaderboard(self, event_id: Optional[int] = None,
                        bib_number: Optional[int] = None) -> List[Dict[str, Any]]:
        """Standings, best first. With bib_number: just that runner's row (for live updates, #71)."""
        eid = self._eid(event_id)
        ev = self.get_event(eid)
        max_rest = ev.get("max_lap_rest_seconds")
        if max_rest is None:
            max_rest = 600.0
        track_m = ev.get("track_length_m") or 400.0
        now = clock.now()

        with self.connect() as conn:
            rows = conn.execute("""
                SELECT
                    p.bib_number,
                    p.name,
                    p.start_time,
                    p.last_crossing_time,
                    COUNT(l.id) AS total_laps,
                    MAX(l.crossing_time) AS last_crossing_time_laps,
                    MIN(l.lap_duration_seconds) AS fastest_lap_seconds,
                    (SELECT lap_duration_seconds FROM laps
                      WHERE event_id = p.event_id AND bib_number = p.bib_number
                      ORDER BY lap_number DESC LIMIT 1) AS last_lap_seconds,
                    COALESCE(SUM(l.lap_duration_seconds), 0.0) AS total_time_seconds,
                    COALESCE((SELECT SUM(duration_seconds) FROM breaks
                      WHERE event_id = p.event_id AND bib_number = p.bib_number), 0.0) AS total_rest_seconds
                FROM participants p
                LEFT JOIN laps l ON l.event_id = p.event_id AND l.bib_number = p.bib_number
                WHERE p.event_id = ? AND (? IS NULL OR p.bib_number = ?)
                GROUP BY p.bib_number, p.name
                ORDER BY total_laps DESC, (last_crossing_time_laps IS NULL), last_crossing_time_laps ASC;""",
                (eid, bib_number, bib_number)).fetchall()
            on_break = {r["bib_number"] for r in conn.execute(
                "SELECT bib_number FROM breaks WHERE event_id = ? AND end_time = 0.0;", (eid,)).fetchall()}

        results = []
        for r in rows:
            item = dict(r)
            item["distance_m"] = round(item["total_laps"] * track_m, 1)
            if not item["start_time"]:
                item["status"] = "not_started"
            elif ev["status"] == "ended":
                item["status"] = "finished"
            elif item["bib_number"] in on_break:
                item["status"] = "on_break"
            elif max_rest > 0 and item["last_crossing_time"] and (now - item["last_crossing_time"]) > max_rest:
                item["status"] = "on_break"
            elif ev["status"] == "running":
                item["status"] = "active"
            else:
                item["status"] = "idle"
            results.append(item)
        return results

    def reset_race_data(self, event_id: Optional[int] = None):
        """Clears laps, breaks and raw reads and returns the event to 'setup'. Registrations are kept."""
        eid = self._eid(event_id)
        with self.connect() as conn:
            for table in ("laps", "breaks", "raw_reads"):
                conn.execute(f"DELETE FROM {table} WHERE event_id = ?;", (eid,))
            conn.execute("""UPDATE participants SET start_time = NULL, last_crossing_time = NULL, start_flag_reason = NULL
                            WHERE event_id = ?;""", (eid,))
            conn.execute("UPDATE events SET status = 'setup', start_time = NULL, end_time = NULL WHERE id = ?;", (eid,))

    def reset_all_event_data(self, event_id: Optional[int] = None):
        """Completely clears laps, breaks, raw reads, and registered participants for the event, returning it to 'setup'."""
        eid = self._eid(event_id)
        with self.connect() as conn:
            for table in ("laps", "breaks", "raw_reads", "participants"):
                conn.execute(f"DELETE FROM {table} WHERE event_id = ?;", (eid,))
            conn.execute("UPDATE events SET status = 'setup', start_time = NULL, end_time = NULL WHERE id = ?;", (eid,))

    def seed_demo_runners(self):
        """Fills the *current* event with sample runners, but only if it has no participants yet."""
        if self.get_all_participants():
            return
        demo = [
            (101, "Alex Chen", "E28011700000020101010001", "Sub-60s target"),
            (102, "Sarah Connor", "E28011700000020101010002", "Steady pacer"),
            (103, "Marcus Johnson", "E28011700000020101010003", "Track newcomer"),
            (104, "Elena Rostova", "E28011700000020101010004", "Sprinter"),
            (105, "Dave K.", "E28011700000020101010005", "Jogger"),
        ]
        for bib, name, epc, notes in demo:
            self.add_participant(bib, name, epc, notes)
