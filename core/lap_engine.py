import time
import random
import asyncio
import logging
from typing import Dict, List, Tuple, Callable, Optional, Any
from core.db import Database
from core.models import TagRead

logger = logging.getLogger("LapEngine")

class LapEngine:
    def __init__(self, db: Database, 
                 crossing_window: float = 2.0, 
                 on_lap_completed: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_runner_started: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_runner_resumed: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_tag_detected: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.db = db
        self.crossing_window = crossing_window
        self.on_lap_completed = on_lap_completed
        self.on_runner_started = on_runner_started
        self.on_runner_resumed = on_runner_resumed
        self.on_tag_detected = on_tag_detected
        self.speed_multiplier: float = 1.0
        
        # Buffer to aggregate bursts of reads: epc -> list of (timestamp, rssi)
        self.buffers: Dict[str, List[Tuple[float, float]]] = {}

    def process_tag_read(self, tag_read: TagRead):
        """Called whenever the RFID reader (mock or physical) reads an EPC."""
        epc = tag_read.epc.upper().strip()
        now = tag_read.timestamp

        # Notify any listeners that an RFID tag was detected (useful for Admin registration)
        if self.on_tag_detected:
            self.on_tag_detected({
                "epc": epc,
                "timestamp": now,
                "rssi": tag_read.rssi
            })

        if epc not in self.buffers:
            self.buffers[epc] = []
        self.buffers[epc].append((now, tag_read.rssi))

    async def flush_loop(self):
        """Background coroutine that checks buffers and triggers crossings when a burst finishes."""
        while True:
            await asyncio.sleep(0.5)
            now = time.time()
            epcs_to_process = []

            for epc, reads in list(self.buffers.items()):
                if not reads:
                    continue
                last_read_time = reads[-1][0]
                # If no new read for crossing_window seconds, runner has exited antenna beam
                if (now - last_read_time) >= self.crossing_window:
                    epcs_to_process.append(epc)

            for epc in epcs_to_process:
                reads = self.buffers.pop(epc, [])
                if reads:
                    await self._resolve_crossing(epc, reads)

    async def _resolve_crossing(self, epc: str, reads: List[Tuple[float, float]], force: bool = False):
        """Determines peak timestamp and updates participant state (start, lap, or break resume)."""
        peak_time, peak_rssi = max(reads, key=lambda r: r[1])

        event_state = self.db.get_event_state()
        min_lap_sec = event_state.get("min_lap_seconds", 45.0)
        max_rest_sec = event_state.get("max_lap_rest_seconds", 600.0)

        effective_min_lap = min_lap_sec / max(0.1, self.speed_multiplier)
        effective_max_rest = max_rest_sec / max(0.1, self.speed_multiplier)

        participant = self.db.get_participant_by_epc(epc)
        if not participant:
            logger.info(f"Tag read for unassigned EPC: {epc} (Peak RSSI: {peak_rssi:.1f} dBm)")
            return

        bib = participant["bib_number"]
        name = participant["name"]

        # Only process if race event is active
        if not event_state.get("is_active"):
            logger.info(f"Event paused/inactive: {name} (Bib {bib}) crossed mat.")
            return

        race_start = event_state.get("start_time")
        if not race_start:
            logger.warning(f"Event active but no start_time set!")
            return

        runner_start = participant.get("start_time")

        # -------------------------------------------------------------
        # CASE 1: Runner has NOT started yet -> Record CHIP START (Lap 0)
        # -------------------------------------------------------------
        if runner_start is None:
            self.db.set_participant_start(bib, peak_time)
            logger.info(f"🚀 CHIP START: Bib {bib} ({name}) started running at {peak_time:.2f} (RSSI: {peak_rssi:.1f} dBm)")
            
            if self.on_runner_started:
                start_data = {
                    "bib_number": bib,
                    "name": name,
                    "start_time": peak_time,
                    "event_offset_seconds": round(peak_time - race_start, 1),
                    "peak_rssi": round(peak_rssi, 1)
                }
                if asyncio.iscoroutinefunction(self.on_runner_started):
                    await self.on_runner_started(start_data)
                else:
                    self.on_runner_started(start_data)
            return

        # -------------------------------------------------------------
        # CASE 2: Runner HAS started -> Evaluate Lap or Break Resume
        # -------------------------------------------------------------
        last_crossing = participant.get("last_crossing_time") or runner_start
        time_since_last = peak_time - last_crossing

        if not force:
            # Debounce check (runner lingering near antenna)
            if time_since_last < effective_min_lap:
                logger.info(f"Debounce: Bib {bib} ({name}) read ignored. Only {time_since_last:.1f}s since last crossing (min {effective_min_lap:.1f}s).")
                return

            # Break / Rest detection: if time since last crossing exceeds threshold
            if time_since_last > effective_max_rest:
                break_duration = time_since_last * self.speed_multiplier
                self.db.record_break(
                    bib_number=bib,
                    start_time=last_crossing,
                    end_time=peak_time,
                    duration_seconds=round(break_duration, 2)
                )
                self.db.update_participant_last_crossing(bib, peak_time)
                logger.info(f"☕ BREAK DETECTED & RESUMED: Bib {bib} ({name}) resumed after {break_duration/60:.1f}m break!")

                if self.on_runner_resumed:
                    resumed_data = {
                        "bib_number": bib,
                        "name": name,
                        "break_duration_seconds": round(break_duration, 2),
                        "crossing_time": peak_time
                    }
                    if asyncio.iscoroutinefunction(self.on_runner_resumed):
                        await self.on_runner_resumed(resumed_data)
                    else:
                        self.on_runner_resumed(resumed_data)
                return

            # Normal lap duration
            lap_duration = max(30.0, time_since_last * self.speed_multiplier)
        else:
            # Force manual trigger from UI
            lap_duration = (time_since_last * self.speed_multiplier) if time_since_last >= 15.0 else random.uniform(62.0, 85.0)

        # Record completed lap
        last_lap = self.db.get_last_lap(bib)
        lap_number = (last_lap["lap_number"] + 1) if last_lap else 1

        lap_id = self.db.record_lap(
            bib_number=bib,
            lap_number=lap_number,
            crossing_time=peak_time,
            lap_duration_seconds=round(lap_duration, 2),
            peak_rssi=round(peak_rssi, 1),
            manual_entry=force
        )
        self.db.update_participant_last_crossing(bib, peak_time)

        logger.info(f"🏁 LAP RECORDED: Bib {bib} ({name}) - Lap {lap_number} in {lap_duration:.2f}s (RSSI: {peak_rssi:.1f} dBm)")

        if self.on_lap_completed:
            lap_data = {
                "id": lap_id,
                "bib_number": bib,
                "name": name,
                "lap_number": lap_number,
                "lap_duration_seconds": round(lap_duration, 2),
                "crossing_time": peak_time,
                "peak_rssi": round(peak_rssi, 1)
            }
            if asyncio.iscoroutinefunction(self.on_lap_completed):
                await self.on_lap_completed(lap_data)
            else:
                self.on_lap_completed(lap_data)

    async def trigger_manual_crossing(self, bib_number: int, force: bool = True) -> Optional[Dict[str, Any]]:
        """Allows admin UI to manually trigger an immediate crossing for testing or missed bib."""
        participant = self.db.get_participant_by_bib(bib_number)
        if not participant:
            return None
        epc = participant["epc"]
        now = time.time()
        reads = [(now, -42.0)]
        await self._resolve_crossing(epc, reads, force=force)
        return participant
