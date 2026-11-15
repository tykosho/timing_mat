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

@dataclass
class TagRead:
    epc: str
    timestamp: float
    rssi: float = 0.0

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
