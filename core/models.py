from dataclasses import dataclass
from typing import Optional

@dataclass
class Participant:
    bib_number: int
    name: str
    epc: str
    notes: Optional[str] = ""
    start_time: Optional[float] = None
    last_crossing_time: Optional[float] = None

@dataclass
class Lap:
    bib_number: int
    lap_number: int
    crossing_time: float
    lap_duration_seconds: float
    id: Optional[int] = None
    peak_rssi: float = 0.0
    manual_entry: bool = False

@dataclass
class Break:
    id: Optional[int] = None
    bib_number: int = 0
    break_number: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    duration_seconds: float = 0.0

@dataclass
class LeaderboardEntry:
    bib_number: int
    name: str
    total_laps: int
    status: str = "not_started"
    start_time: Optional[float] = None
    last_lap_seconds: Optional[float] = None
    fastest_lap_seconds: Optional[float] = None
    total_time_seconds: Optional[float] = None
    total_rest_seconds: Optional[float] = 0.0
    last_crossing_time: Optional[float] = None

def apriltag_to_epc(tag_id: int) -> str:
    """Canonical 16-hex-char virtual EPC for an AprilTag 16h5 ID (0..29)."""
    tid = int(tag_id)
    if not (0 <= tid < 30):
        raise ValueError(f"AprilTag 16h5 ID must be 0..29, got {tid}")
    return f"A71605000000{tid:04X}"


def epc_to_apriltag(epc: Optional[str]) -> Optional[int]:
    """Extract AprilTag ID if `epc` is a virtual AprilTag EPC, else None."""
    if not epc:
        return None
    clean = epc.strip().upper()
    if len(clean) == 16 and clean.startswith("A71605000000"):
        try:
            tid = int(clean[12:], 16)
            return tid if 0 <= tid < 30 else None
        except ValueError:
            return None
    return None


@dataclass
class TagRead:
    epc: str
    timestamp: float
    rssi: float = 0.0
    source: str = "rfid"
    apriltag_id: Optional[int] = None

@dataclass
class EventStatus:
    name: str
    is_active: bool
    start_time: Optional[float] = None
    elapsed_seconds: float = 0.0
    total_laps_recorded: int = 0
    total_participants: int = 0
    min_lap_seconds: float = 45.0
    max_lap_rest_seconds: float = 600.0
