import asyncio
import logging
from collections import deque
from typing import Deque, Dict, List, Tuple, Callable, Optional, Any
from core.db import Database
from core import clock
from core.models import TagRead

logger = logging.getLogger("LapEngine")

# Longest a single crossing's burst of reads may last before it's resolved anyway (issue #11).
# A runner passing the mat is in the antenna field for 1-2 s.
MAX_BURST_SECONDS = 8.0

class LapEngine:
    def __init__(self, db: Database, 
                 crossing_window: float = 2.0, 
                 on_lap_completed: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_runner_started: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_runner_resumed: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_tag_detected: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_unassigned_tag: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.db = db
        self.crossing_window = crossing_window
        self.on_lap_completed = on_lap_completed
        self.on_runner_started = on_runner_started
        self.on_runner_resumed = on_runner_resumed
        self.on_tag_detected = on_tag_detected
        self.on_unassigned_tag = on_unassigned_tag
        self.speed_multiplier: float = 1.0

        # Buffer to aggregate bursts of reads: epc -> list of (timestamp, rssi)
        self.buffers: Dict[str, List[Tuple[float, float]]] = {}

        # Raw read log. The reader thread appends (deque.append is thread-safe) and flush_loop
        # writes batches to the DB. Only reads while an event is running/paused are kept.
        self.raw_queue: Deque[Tuple[int, str, float, float]] = deque()
        self.log_event_id: Optional[int] = None

    def refresh_event(self):
        """Re-reads the current event so raw reads are tagged with the right event (or dropped)."""
        event = self.db.get_event()
        self.log_event_id = event["id"] if event and event["status"] in ("running", "paused") else None

    def maintain_clock(self):
        """Keeps the race clock honest about system clock changes (see core/clock.py)."""
        clock.clock.maintain(self.db.race_in_progress(), self.db.latest_timestamp())

    def flush_raw_reads(self):
        """Writes queued raw reads to the database."""
        batch = []
        while self.raw_queue:
            batch.append(self.raw_queue.popleft())
        if batch:
            self.db.insert_raw_reads(batch)

    def process_tag_read(self, tag_read: TagRead):
        """Called for every read from the RFID reader (mock or physical).

        Must run on the event loop, like flush_once: web/app.py hands reads over from the
        reader thread with call_soon_threadsafe, so the buffers have a single owner (issue #13).
        """
        epc = tag_read.epc.upper().strip()
        now = tag_read.timestamp

        # Notify any listeners that an RFID tag was detected (useful for Admin registration)
        if self.on_tag_detected:
            self.on_tag_detected({
                "epc": epc,
                "timestamp": now,
                "rssi": tag_read.rssi
            })

        event_id = self.log_event_id
        if event_id is not None:
            self.raw_queue.append((event_id, epc, now, tag_read.rssi))

        if epc not in self.buffers:
            self.buffers[epc] = []
        self.buffers[epc].append((now, tag_read.rssi))

    async def flush_loop(self):
        """Background coroutine that checks buffers and triggers crossings when a burst finishes."""
        ticks = 0
        while True:
            await asyncio.sleep(0.2)
            self.refresh_event()
            self.flush_raw_reads()
            ticks += 1
            if ticks % 150 == 0:  # every ~30 s
                self.maintain_clock()
            await self.flush_once(clock.now())

    async def flush_once(self, now: float):
        """Resolves every burst of reads that has finished (or gone on too long) by `now`."""
        effective_window = max(0.15, self.crossing_window / max(1.0, self.speed_multiplier))
        epcs_to_process = []

        max_burst = MAX_BURST_SECONDS / max(1.0, self.speed_multiplier)
        for epc, reads in list(self.buffers.items()):
            if not reads:
                continue
            last_read_time = reads[-1][0]
            # If no new read for effective_window seconds, runner has exited antenna beam.
            # A tag that never leaves (spare bib by the mat, runner standing on it) is
            # resolved anyway once its burst is MAX_BURST_SECONDS old; later reads start a
            # new burst, which the min-lap debounce then ignores.
            if (now - last_read_time) >= effective_window or (now - reads[0][0]) >= max_burst:
                epcs_to_process.append(epc)

        for epc in epcs_to_process:
            reads = self.buffers.pop(epc, [])
            if reads:
                await self._resolve_crossing(epc, reads)

    async def _resolve_crossing(self, epc: str, reads: List[Tuple[float, float]], force: bool = False) -> Optional[str]:
        """Determines peak timestamp and updates participant state (start, lap, or break resume).

        Returns what happened: "started", "lap", "resumed", "debounced", or None when the
        crossing was ignored (unknown tag, event not running).
        """
        peak_time, peak_rssi = max(reads, key=lambda r: r[1])

        event_state = self.db.get_event_state()
        min_lap_sec = event_state.get("min_lap_seconds", 45.0)
        max_rest_sec = event_state.get("max_lap_rest_seconds", 600.0)

        effective_min_lap = min_lap_sec / max(0.1, self.speed_multiplier)
        effective_max_rest = max_rest_sec / max(0.1, self.speed_multiplier)

        participant = self.db.get_participant_by_epc(epc)
        if not participant:
            logger.info(f"Tag read for unassigned EPC: {epc} (Peak RSSI: {peak_rssi:.1f} dBm)")
            if self.on_unassigned_tag:
                tag_data = {
                    "epc": epc,
                    "peak_rssi": round(peak_rssi, 1),
                    "timestamp": peak_time,
                }
                if asyncio.iscoroutinefunction(self.on_unassigned_tag):
                    await self.on_unassigned_tag(tag_data)
                else:
                    self.on_unassigned_tag(tag_data)
            return None

        bib = participant["bib_number"]
        name = participant["name"]

        # Only time crossings while the event is running. A crossing that finished just before
        # End was pressed can still be resolved here (bursts are flushed ~2s after the last read).
        status = event_state.get("status")
        ended_in_time = status == "ended" and event_state.get("end_time") and peak_time <= event_state["end_time"]
        if status != "running" and not ended_in_time:
            logger.info(f"Event {status}: {name} (Bib {bib}) crossed mat; not timed.")
            return None

        race_start = event_state.get("start_time")
        if not race_start:
            logger.warning("Event active but no start_time set!")
            return None

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
            return "started"

        # -------------------------------------------------------------
        # CASE 2: Runner HAS started -> Evaluate Lap or Break Resume
        # -------------------------------------------------------------
        last_crossing = participant.get("last_crossing_time") or runner_start
        time_since_last = peak_time - last_crossing

        # Debounce check (runner lingering near antenna). This applies to forced crossings too:
        # a lap time is always the real time since the last crossing, never an invented one.
        if time_since_last < effective_min_lap:
            logger.info(f"Debounce: Bib {bib} ({name}) {'forced crossing rejected' if force else 'read ignored'}. Only {time_since_last:.1f}s since last crossing (min {effective_min_lap:.1f}s).")
            return "debounced"

        if not force:
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
                return "resumed"

            # Normal lap duration. (No floor: the min-lap debounce above already rejects short ones.)
            lap_duration = time_since_last * self.speed_multiplier
        else:
            # Forced crossing from the admin UI: skip break detection, but time it for real.
            lap_duration = time_since_last * self.speed_multiplier

        # Record completed lap
        last_lap = self.db.get_last_lap(bib)
        lap_number = (last_lap["lap_number"] + 1) if last_lap else 1

        # Check for suspicious long lap (possible missed reading: >= 1.75x previous lap if >= 2 laps exist)
        flag_reason = None
        prior_laps = self.db.get_laps_for_runner(bib)
        if len(prior_laps) >= 2 and not force:
            durations = sorted([l["lap_duration_seconds"] for l in prior_laps])
            median_dur = durations[len(durations) // 2]
            if lap_duration >= (1.75 * median_dur) and median_dur >= 20.0:
                flag_reason = f"Missed reading suspected ({round(lap_duration, 1)}s vs median {round(median_dur, 1)}s)"
                logger.warning(f"⚠️ SUSPICIOUS LAP: Bib {bib} ({name}) Lap {lap_number} flagged: {flag_reason}")

        lap_id = self.db.record_lap(
            bib_number=bib,
            lap_number=lap_number,
            crossing_time=peak_time,
            lap_duration_seconds=round(lap_duration, 2),
            peak_rssi=round(peak_rssi, 1),
            manual_entry=force,
            flag_reason=flag_reason
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
        return "lap"

    async def trigger_manual_crossing(self, bib_number: int, force: bool = True) -> Optional[Dict[str, Any]]:
        """Allows admin UI to manually trigger an immediate crossing for testing or missed bib.

        Returns None if the bib doesn't exist, otherwise {"participant": ..., "outcome": ...}
        where outcome is the value returned by _resolve_crossing.
        """
        participant = self.db.get_participant_by_bib(bib_number)
        if not participant:
            return None
        epc = participant["epc"]
        now = clock.now()
        reads = [(now, -42.0)]
        outcome = await self._resolve_crossing(epc, reads, force=force)
        return {"participant": participant, "outcome": outcome}
