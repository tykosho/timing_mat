import os
import sqlite3
import time
from typing import Optional, List, Dict, Any

class Database:
    def __init__(self, db_path: str = "data/race.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Participants table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                bib_number INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                epc TEXT UNIQUE NOT NULL,
                notes TEXT DEFAULT '',
                created_at REAL NOT NULL,
                start_time REAL DEFAULT NULL,
                last_crossing_time REAL DEFAULT NULL
            );
            """)

            # Laps table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS laps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bib_number INTEGER NOT NULL,
                lap_number INTEGER NOT NULL,
                crossing_time REAL NOT NULL,
                lap_duration_seconds REAL NOT NULL,
                peak_rssi REAL DEFAULT 0.0,
                manual_entry INTEGER DEFAULT 0,
                FOREIGN KEY(bib_number) REFERENCES participants(bib_number) ON DELETE CASCADE
            );
            """)

            # Breaks / Rest periods table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS breaks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bib_number INTEGER NOT NULL,
                break_number INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                duration_seconds REAL NOT NULL,
                FOREIGN KEY(bib_number) REFERENCES participants(bib_number) ON DELETE CASCADE
            );
            """)

            # Event state (single row)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS event_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT NOT NULL,
                is_active INTEGER DEFAULT 0,
                start_time REAL,
                min_lap_seconds REAL DEFAULT 45.0,
                max_lap_rest_seconds REAL DEFAULT 600.0
            );
            """)

            # Migrations for existing databases
            try:
                cursor.execute("ALTER TABLE participants ADD COLUMN start_time REAL DEFAULT NULL;")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE participants ADD COLUMN last_crossing_time REAL DEFAULT NULL;")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE event_state ADD COLUMN max_lap_rest_seconds REAL DEFAULT 600.0;")
            except sqlite3.OperationalError:
                pass

            # Initialize event state if not present
            cursor.execute("SELECT id FROM event_state WHERE id = 1;")
            if not cursor.fetchone():
                cursor.execute("""
                INSERT INTO event_state (id, name, is_active, start_time, min_lap_seconds, max_lap_rest_seconds)
                VALUES (1, 'Take Back the Track!', 0, NULL, 45.0, 600.0);
                """)

            conn.commit()

    # --- Event State Methods ---
    def get_event_state(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM event_state WHERE id = 1;").fetchone()
            if row:
                return dict(row)
            return {
                "name": "Take Back the Track!", 
                "is_active": 0, 
                "start_time": None, 
                "min_lap_seconds": 45.0,
                "max_lap_rest_seconds": 600.0
            }

    def update_event_state(self, is_active: Optional[bool] = None, start_time: Optional[float] = None, 
                           name: Optional[str] = None, min_lap_seconds: Optional[float] = None,
                           max_lap_rest_seconds: Optional[float] = None):
        current = self.get_event_state()
        new_active = current["is_active"] if is_active is None else (1 if is_active else 0)
        new_start = current["start_time"] if start_time is None else start_time
        new_name = current["name"] if name is None else name
        new_min_lap = current["min_lap_seconds"] if min_lap_seconds is None else min_lap_seconds
        new_max_rest = current.get("max_lap_rest_seconds", 600.0) if max_lap_rest_seconds is None else max_lap_rest_seconds

        with self.get_connection() as conn:
            conn.execute("""
            UPDATE event_state 
            SET name = ?, is_active = ?, start_time = ?, min_lap_seconds = ?, max_lap_rest_seconds = ?
            WHERE id = 1;
            """, (new_name, new_active, new_start, new_min_lap, new_max_rest))
            conn.commit()

    # --- Participant Methods ---
    def add_participant(self, bib_number: int, name: str, epc: str, notes: str = "") -> bool:
        with self.get_connection() as conn:
            try:
                conn.execute("""
                INSERT INTO participants (bib_number, name, epc, notes, created_at)
                VALUES (?, ?, ?, ?, ?);
                """, (bib_number, name.strip(), epc.strip().upper(), notes.strip(), time.time()))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def update_participant(self, bib_number: int, name: str, epc: str, notes: str = "") -> bool:
        with self.get_connection() as conn:
            try:
                conn.execute("""
                UPDATE participants
                SET name = ?, epc = ?, notes = ?
                WHERE bib_number = ?;
                """, (name.strip(), epc.strip().upper(), notes.strip(), bib_number))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def delete_participant(self, bib_number: int) -> bool:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM laps WHERE bib_number = ?;", (bib_number,))
            conn.execute("DELETE FROM breaks WHERE bib_number = ?;", (bib_number,))
            conn.execute("DELETE FROM participants WHERE bib_number = ?;", (bib_number,))
            conn.commit()
            return True

    def set_participant_start(self, bib_number: int, start_time: float):
        with self.get_connection() as conn:
            conn.execute("""
            UPDATE participants 
            SET start_time = ?, last_crossing_time = ?
            WHERE bib_number = ?;
            """, (start_time, start_time, bib_number))
            conn.commit()

    def update_participant_last_crossing(self, bib_number: int, crossing_time: float):
        with self.get_connection() as conn:
            conn.execute("""
            UPDATE participants 
            SET last_crossing_time = ?
            WHERE bib_number = ?;
            """, (crossing_time, bib_number))
            conn.commit()

    def reset_participant_start(self, bib_number: int):
        with self.get_connection() as conn:
            conn.execute("""
            UPDATE participants 
            SET start_time = NULL, last_crossing_time = NULL
            WHERE bib_number = ?;
            """, (bib_number,))
            conn.execute("DELETE FROM laps WHERE bib_number = ?;", (bib_number,))
            conn.execute("DELETE FROM breaks WHERE bib_number = ?;", (bib_number,))
            conn.commit()

    def get_participant_by_epc(self, epc: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM participants WHERE UPPER(epc) = UPPER(?);", (epc.strip(),)).fetchone()
            return dict(row) if row else None

    def get_participant_by_bib(self, bib_number: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM participants WHERE bib_number = ?;", (bib_number,)).fetchone()
            return dict(row) if row else None

    def get_all_participants(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM participants ORDER BY bib_number ASC;").fetchall()
            return [dict(r) for r in rows]

    # --- Lap Methods ---
    def record_lap(self, bib_number: int, lap_number: int, crossing_time: float, 
                   lap_duration_seconds: float, peak_rssi: float = 0.0, manual_entry: bool = False) -> int:
        with self.get_connection() as conn:
            cursor = conn.execute("""
            INSERT INTO laps (bib_number, lap_number, crossing_time, lap_duration_seconds, peak_rssi, manual_entry)
            VALUES (?, ?, ?, ?, ?, ?);
            """, (bib_number, lap_number, crossing_time, lap_duration_seconds, peak_rssi, 1 if manual_entry else 0))
            conn.commit()
            return cursor.lastrowid

    def get_last_lap(self, bib_number: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("""
            SELECT * FROM laps 
            WHERE bib_number = ? 
            ORDER BY lap_number DESC 
            LIMIT 1;
            """, (bib_number,)).fetchone()
            return dict(row) if row else None

    def get_laps_for_runner(self, bib_number: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("""
            SELECT * FROM laps 
            WHERE bib_number = ? 
            ORDER BY lap_number ASC;
            """, (bib_number,)).fetchall()
            return [dict(r) for r in rows]

    def delete_lap(self, lap_id: int) -> bool:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM laps WHERE id = ?;", (lap_id,))
            conn.commit()
            return True

    def get_total_laps_count(self) -> int:
        with self.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM laps;").fetchone()
            return row["count"] if row else 0

    # --- Breaks / Rest Period Methods ---
    def record_break(self, bib_number: int, start_time: float, end_time: float, duration_seconds: float) -> int:
        with self.get_connection() as conn:
            # Determine break number
            row = conn.execute("SELECT COUNT(*) as count FROM breaks WHERE bib_number = ?;", (bib_number,)).fetchone()
            break_num = (row["count"] + 1) if row else 1

            cursor = conn.execute("""
            INSERT INTO breaks (bib_number, break_number, start_time, end_time, duration_seconds)
            VALUES (?, ?, ?, ?, ?);
            """, (bib_number, break_num, start_time, end_time, duration_seconds))
            conn.commit()
            return cursor.lastrowid

    def get_breaks_for_runner(self, bib_number: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("""
            SELECT * FROM breaks 
            WHERE bib_number = ? 
            ORDER BY break_number ASC;
            """, (bib_number,)).fetchall()
            return [dict(r) for r in rows]

    def get_open_break(self, bib_number: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM breaks WHERE bib_number = ? AND end_time = 0.0 ORDER BY id DESC LIMIT 1;", (bib_number,)).fetchone()
            return dict(row) if row else None

    def start_manual_break(self, bib_number: int) -> Dict[str, Any]:
        now = time.time()
        with self.get_connection() as conn:
            open_b = self.get_open_break(bib_number)
            if not open_b:
                count_row = conn.execute("SELECT COUNT(*) as count FROM breaks WHERE bib_number = ?;", (bib_number,)).fetchone()
                break_num = (count_row["count"] + 1) if count_row else 1
                conn.execute("""
                INSERT INTO breaks (bib_number, break_number, start_time, end_time, duration_seconds)
                VALUES (?, ?, ?, 0.0, 0.0);
                """, (bib_number, break_num, now))
                conn.commit()
            return {"status": "on_break", "bib": bib_number, "start_time": now}

    def end_manual_break(self, bib_number: int) -> Dict[str, Any]:
        now = time.time()
        with self.get_connection() as conn:
            open_b = self.get_open_break(bib_number)
            duration = 0.0
            if open_b:
                start_time = open_b["start_time"]
                duration = max(0.0, now - start_time)
                conn.execute("""
                UPDATE breaks 
                SET end_time = ?, duration_seconds = ?
                WHERE id = ?;
                """, (now, round(duration, 2), open_b["id"]))
            conn.execute("UPDATE participants SET last_crossing_time = ? WHERE bib_number = ?;", (now, bib_number))
            conn.commit()
            return {"status": "resumed", "bib": bib_number, "duration_seconds": round(duration, 2)}

    # --- Leaderboard Aggregations ---
    def get_leaderboard(self) -> List[Dict[str, Any]]:
        event_state = self.get_event_state()
        max_rest = event_state.get("max_lap_rest_seconds", 600.0)
        now = time.time()

        with self.get_connection() as conn:
            query = """
            SELECT 
                p.bib_number,
                p.name,
                p.start_time,
                p.last_crossing_time,
                COUNT(l.id) as total_laps,
                MAX(l.crossing_time) as last_crossing_time_laps,
                MIN(l.lap_duration_seconds) as fastest_lap_seconds,
                (SELECT lap_duration_seconds FROM laps WHERE bib_number = p.bib_number ORDER BY lap_number DESC LIMIT 1) as last_lap_seconds,
                COALESCE(SUM(l.lap_duration_seconds), 0.0) as total_time_seconds,
                COALESCE((SELECT SUM(duration_seconds) FROM breaks WHERE bib_number = p.bib_number), 0.0) as total_rest_seconds
            FROM participants p
            LEFT JOIN laps l ON p.bib_number = l.bib_number
            GROUP BY p.bib_number, p.name
            ORDER BY total_laps DESC, (last_crossing_time_laps IS NULL), last_crossing_time_laps ASC;
            """
            rows = conn.execute(query).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                # Compute runner status
                if not item["start_time"]:
                    item["status"] = "not_started"
                elif self.get_open_break(item["bib_number"]) is not None:
                    item["status"] = "on_break"
                elif item["last_crossing_time"] and (now - item["last_crossing_time"]) > max_rest:
                    item["status"] = "on_break"
                elif event_state.get("is_active"):
                    item["status"] = "active"
                else:
                    item["status"] = "idle"

                results.append(item)
            return results

    def reset_race_data(self):
        """Clears all laps and breaks, resetting runner start times while preserving participants."""
        with self.get_connection() as conn:
            conn.execute("DELETE FROM laps;")
            conn.execute("DELETE FROM breaks;")
            conn.execute("UPDATE participants SET start_time = NULL, last_crossing_time = NULL;")
            conn.execute("UPDATE event_state SET is_active = 0, start_time = NULL WHERE id = 1;")
            conn.commit()

    def seed_demo_runners(self):
        """Pre-populates participants with sample bibs and realistic EPC hex strings."""
        demo = [
            (101, "Alex Chen", "E28011700000020101010001", "Sub-60s target"),
            (102, "Sarah Connor", "E28011700000020101010002", "Steady pacer"),
            (103, "Marcus Johnson", "E28011700000020101010003", "Track newcomer"),
            (104, "Elena Rostova", "E28011700000020101010004", "Sprinter"),
            (105, "Dave K.", "E28011700000020101010005", "Jogger"),
        ]
        for bib, name, epc, notes in demo:
            if not self.get_participant_by_bib(bib):
                self.add_participant(bib, name, epc, notes)
