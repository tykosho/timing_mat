import os
import re
import time
import json
import asyncio
import logging
import yaml
from markupsafe import Markup
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core import exports
from core.db import Database, RegistrationError, EventStateError
from core.registration_import import parse_registration_csv
from core.lap_engine import LapEngine
from hardware.mock_reader import MockReader
from hardware.serial_reader import SerialReader

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("RaceApp")

# Load configuration
config_path = os.getenv("CONFIG_PATH", "config.yaml")
config = {}
if os.path.exists(config_path):
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

def _flag(env_name: str, config_value: Any, default: bool) -> bool:
    env = os.getenv(env_name)
    if env is not None:
        return env.lower() in ("true", "1", "yes")
    return default if config_value is None else bool(config_value)

# Hardware setup (Mock vs Serial)
use_mock = _flag("MOCK_HARDWARE", config.get("hardware", {}).get("mock"), True)
serial_port = os.getenv("SERIAL_PORT", config.get("hardware", {}).get("serial_port", "/dev/ttyUSB0"))
baud_rate = int(os.getenv("BAUD_RATE", config.get("hardware", {}).get("baud_rate", 115200)))

db_path = os.getenv("DB_PATH", config.get("database", {}).get("path", "data/race.db"))
db = Database(db_path, default_event_name=config.get("event", {}).get("name", "Take Back the Track!"))

# Demo runners match the simulator's tags, so they default to on only in mock mode.
# Never seeded on real hardware unless explicitly asked for.
if _flag("SEED_DEMO", config.get("event", {}).get("seed_demo"), use_mock):
    db.seed_demo_runners()

# Active WebSocket connections
active_connections: List[WebSocket] = []

async def broadcast_ws(message: Dict[str, Any]):
    disconnected = []
    for ws in active_connections:
        try:
            await ws.send_json(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in active_connections:
            active_connections.remove(ws)

# Callback when a lap is completed
async def on_lap_completed(lap_data: Dict[str, Any]):
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "lap_completed",
        "lap": lap_data,
        "leaderboard": leaderboard
    })

# Callback when a runner begins (Chip Start / Lap 0)
async def on_runner_started(start_data: Dict[str, Any]):
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "runner_started",
        "data": start_data,
        "leaderboard": leaderboard
    })

# Callback when a runner resumes after a break
async def on_runner_resumed(resume_data: Dict[str, Any]):
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "runner_resumed",
        "data": resume_data,
        "leaderboard": leaderboard
    })

# Callback when any tag is detected (used for Admin auto-fill / registration)
def on_tag_detected(tag_info: Dict[str, Any]):
    if main_event_loop is None:
        return
    asyncio.run_coroutine_threadsafe(
        broadcast_ws({"type": "tag_detected", "data": tag_info}),
        main_event_loop
    )

crossing_window = config.get("event", {}).get("crossing_window_seconds", 2.0)
lap_engine = LapEngine(
    db=db,
    crossing_window=crossing_window,
    on_lap_completed=on_lap_completed,
    on_runner_started=on_runner_started,
    on_runner_resumed=on_runner_resumed,
    on_tag_detected=on_tag_detected
)
lap_engine.refresh_event()

if use_mock:
    reader = MockReader(on_tag_read=lap_engine.process_tag_read, speed_multiplier=1.0)
    logger.info("Operating in MOCK reader mode (Simulated runners active).")
else:
    reader = SerialReader(port=serial_port, baud_rate=baud_rate, on_tag_read=lap_engine.process_tag_read)
    logger.info(f"Operating in PHYSICAL reader mode on {serial_port}.")

main_event_loop = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()

    # Start engine flush loop
    flush_task = asyncio.create_task(lap_engine.flush_loop())

    # Start RFID reader
    reader.start()

    yield

    reader.stop()
    flush_task.cancel()
    lap_engine.flush_raw_reads()

app = FastAPI(title="Race Timing System", lifespan=lifespan)

# Templates and static setup
def _tojson(obj) -> Markup:
    """JSON that's safe to drop inside a <script> tag (names come from registrations)."""
    text = json.dumps(obj)
    for char, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(char, escaped)
    return Markup(text)

templates = Jinja2Templates(directory="web/templates")
templates.env.filters["tojson"] = _tojson
os.makedirs("web/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="web/static"), name="static")

@app.exception_handler(RegistrationError)
async def registration_error_handler(request: Request, exc: RegistrationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})

@app.exception_handler(EventStateError)
async def event_state_error_handler(request: Request, exc: EventStateError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})

async def push_event_status(include_leaderboard: bool = True):
    """Tells the engine and every open page that the event changed."""
    lap_engine.refresh_event()
    message: Dict[str, Any] = {"type": "event_status", "event": db.get_event_state()}
    if include_leaderboard:
        message["leaderboard"] = db.get_leaderboard()
    await broadcast_ws(message)

async def push_leaderboard():
    await broadcast_ws({"type": "leaderboard_update", "leaderboard": db.get_leaderboard()})

# ----------------- PAGE ROUTES -----------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return RedirectResponse(url="/crossings")

@app.get("/crossings", response_class=HTMLResponse)
async def crossings_page(request: Request):
    event_state = db.get_event_state()
    leaderboard = db.get_leaderboard()
    return templates.TemplateResponse(
        request=request,
        name="crossings.html",
        context={
            "event": event_state,
            "leaderboard": leaderboard,
            "is_mock": use_mock
        }
    )

@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="leaderboard.html",
        context={
            "event": db.get_event_state(),
            "leaderboard": db.get_leaderboard(),
            "is_mock": use_mock
        }
    )

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "event": db.get_event_state(),
            "events": db.list_events(),
            "participants": db.get_all_participants(),
            "leaderboard": db.get_leaderboard(),
            "is_mock": use_mock,
            "serial_port": serial_port,
            "raw_read_count": db.count_raw_reads()
        }
    )

@app.get("/prepare", response_class=HTMLResponse)
async def prepare_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="prepare.html",
        context={
            "event": db.get_event_state(),
            "participants": db.get_all_participants(),
            "is_mock": use_mock
        }
    )

@app.get("/replay", response_class=HTMLResponse)
async def replay_page(request: Request):
    event_state = db.get_event_state()
    return templates.TemplateResponse(
        request=request,
        name="replay.html",
        context={
            "event": event_state,
            "is_mock": use_mock
        }
    )

@app.get("/api/replay")
async def get_replay_data():
    """Returns race replay time-series data: crossing timestamps and lap numbers per runner."""
    event_state = db.get_event_state()
    start_time = event_state.get("start_time")
    laps = db.get_all_laps()
    participants = {p["bib_number"]: p["name"] for p in db.get_all_participants()}
    
    runners_data = {}
    for bib, name in participants.items():
        runners_data[bib] = {
            "bib": bib,
            "name": name,
            "points": [{"elapsed": 0.0, "laps": 0}]  # start at 0 laps
        }

    for lap in laps:
        bib = lap["bib_number"]
        if bib not in runners_data:
            runners_data[bib] = {
                "bib": bib,
                "name": lap.get("name") or f"Bib #{bib}",
                "points": [{"elapsed": 0.0, "laps": 0}]
            }
        elapsed = round(lap["crossing_time"] - start_time, 1) if start_time else round(lap["crossing_time"], 1)
        runners_data[bib]["points"].append({
            "elapsed": max(0.0, elapsed),
            "laps": lap["lap_number"],
            "lap_duration": lap["lap_duration_seconds"]
        })

    return {
        "event": event_state,
        "runners": list(runners_data.values()),
        "total_laps": len(laps)
    }

@app.get("/runner/{bib}", response_class=HTMLResponse)
async def runner_page(request: Request, bib: int):
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    event_state = db.get_event_state()
    return templates.TemplateResponse(
        request=request,
        name="runner_detail.html",
        context={
            "runner": participant,
            "laps": db.get_laps_for_runner(bib),
            "breaks": db.get_breaks_for_runner(bib),
            "event": event_state
        }
    )

# ----------------- WEBSOCKET ROUTE -----------------
@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        # Send initial snapshot
        await websocket.send_json({
            "type": "init",
            "event": db.get_event_state(),
            "leaderboard": db.get_leaderboard()
        })
        while True:
            # Keepalive / ping
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_connections:
            active_connections.remove(websocket)

# ----------------- REST API ROUTES -----------------
@app.get("/api/leaderboard")
async def get_leaderboard():
    return db.get_leaderboard()

def _elapsed(state: Dict[str, Any]) -> float:
    if not state["start_time"]:
        return 0.0
    if state["status"] == "ended" and state["end_time"]:
        return state["end_time"] - state["start_time"]
    return time.time() - state["start_time"]

@app.get("/api/event")
async def get_event():
    state = db.get_event_state()
    return {**state, "elapsed_seconds": round(_elapsed(state), 1), "total_laps": db.get_total_laps_count()}

@app.post("/api/event/start")
async def start_event():
    state = db.start_event()
    await push_event_status()
    return {"status": "started", "start_time": state["start_time"], "event": state}

@app.post("/api/event/pause")
async def pause_event():
    state = db.pause_event()
    await push_event_status()
    return {"status": "paused", "event": state}

@app.post("/api/event/end")
async def end_event():
    """Freezes the results. A database backup is written alongside, and results stay available as CSV."""
    state = db.end_event()
    lap_engine.flush_raw_reads()
    backup_path = db.backup("event-ended")
    await push_event_status()
    return {"status": "ended", "event": state, "backup": backup_path}

@app.post("/api/event/reopen")
async def reopen_event():
    state = db.reopen_event()
    await push_event_status()
    return {"status": "paused", "event": state}

@app.post("/api/event/reset")
async def reset_event():
    """Wipes laps/breaks/raw reads for the current event (registrations are kept). A backup is taken first."""
    if db.get_event_state()["status"] == "running":
        raise EventStateError("Pause or end the event before resetting it.")
    lap_engine.flush_raw_reads()
    backup_path = db.backup("pre-reset")
    db.reset_race_data()
    await push_event_status()
    return {"status": "reset", "backup": backup_path}

class SettingsRequest(BaseModel):
    name: Optional[str] = None
    min_lap_seconds: Optional[float] = Field(default=None, gt=0)
    max_lap_rest_seconds: Optional[float] = Field(default=None, gt=0)
    track_length_m: Optional[float] = Field(default=None, gt=0)

@app.post("/api/event/settings")
async def update_settings(req: SettingsRequest):
    db.update_event_settings(
        name=req.name,
        min_lap_seconds=req.min_lap_seconds,
        max_lap_rest_seconds=req.max_lap_rest_seconds,
        track_length_m=req.track_length_m
    )
    await push_event_status(include_leaderboard=False)
    return {"status": "updated", "event": db.get_event_state()}

# Events
class NewEventRequest(BaseModel):
    name: str = Field(min_length=1)

@app.get("/api/events")
async def list_events():
    return db.list_events()

@app.post("/api/events")
async def create_event(req: NewEventRequest):
    """Creates an event and makes it current (so you can start pre-registering straight away)."""
    new_id = db.create_event(req.name)
    db.set_current_event(new_id)
    await push_event_status()
    await broadcast_ws({"type": "event_switched"})
    return {"status": "created", "event": db.get_event(new_id)}

@app.post("/api/events/{event_id}/select")
async def select_event(event_id: int):
    db.set_current_event(event_id)
    await push_event_status()
    await broadcast_ws({"type": "event_switched"})
    return {"status": "selected", "event": db.get_event_state()}

def _csv_response(event_id: int, kind: str, content_fn) -> Response:
    event = db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", event["name"]).strip("-").lower() or "event"
    return Response(
        content=content_fn(db, event_id),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{slug}-{kind}.csv"'}
    )

@app.get("/api/events/{event_id}/results.csv")
async def export_results(event_id: int):
    return _csv_response(event_id, "results", exports.results_csv)

@app.get("/api/events/{event_id}/laps.csv")
async def export_laps(event_id: int):
    return _csv_response(event_id, "laps", exports.laps_csv)

@app.get("/api/events/{event_id}/registrations.csv")
async def export_registrations(event_id: int):
    return _csv_response(event_id, "registrations", exports.registrations_csv)

@app.get("/api/events/{event_id}/raw_reads.csv")
async def export_raw_reads(event_id: int):
    return _csv_response(event_id, "raw-reads", exports.raw_reads_csv)

# Participants API
class ParticipantRequest(BaseModel):
    bib_number: int = Field(gt=0)
    name: str
    epc: Optional[str] = ""
    notes: Optional[str] = ""
    email: Optional[str] = ""

@app.get("/api/participants")
async def list_participants():
    return db.get_all_participants()

@app.post("/api/participants")
async def add_participant(p: ParticipantRequest):
    db.add_participant(p.bib_number, p.name, p.epc, p.notes or "", p.email or "")
    await push_leaderboard()
    return {"status": "created"}

class ImportRequest(BaseModel):
    csv_text: str
    first_bib: int = Field(default=1, gt=0)

@app.post("/api/participants/import")
async def import_participants(req: ImportRequest):
    """Bulk-registers runners from a CSV (e.g. a Google Forms export). Existing registrations are never overwritten."""
    rows, errors = parse_registration_csv(req.csv_text)
    result = db.bulk_add_registrations(rows, first_bib=req.first_bib)
    await push_leaderboard()
    return {**result, "errors": errors}

@app.put("/api/participants/{bib}")
async def edit_participant(bib: int, p: ParticipantRequest):
    db.update_participant(bib, p.name, p.epc, p.notes or "", p.email)
    await push_leaderboard()
    return {"status": "updated"}

class TagRequest(BaseModel):
    epc: str

@app.put("/api/participants/{bib}/tag")
async def assign_tag(bib: int, req: TagRequest):
    """Pairs a scanned tag with a bib. Rejected if another bib in this event already uses the tag."""
    db.set_participant_tag(bib, req.epc)
    await push_leaderboard()
    return {"status": "assigned", "bib": bib, "participant": db.get_participant_by_bib(bib)}

@app.delete("/api/participants/{bib}/tag")
async def clear_tag(bib: int):
    db.set_participant_tag(bib, None)
    await push_leaderboard()
    return {"status": "cleared", "bib": bib}

@app.delete("/api/participants/{bib}")
async def remove_participant(bib: int):
    db.delete_participant(bib)
    await push_leaderboard()
    return {"status": "deleted"}

@app.post("/api/participants/{bib}/start")
async def manual_start_runner(bib: int):
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    now = time.time()
    db.set_participant_start(bib, now)
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "runner_started",
        "data": {"bib_number": bib, "name": participant["name"], "start_time": now},
        "leaderboard": leaderboard
    })
    return {"status": "started", "bib": bib, "start_time": now}

@app.post("/api/participants/{bib}/break/start")
async def start_runner_break(bib: int):
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    result = db.start_manual_break(bib)
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "runner_break",
        "data": {"bib_number": bib, "name": participant["name"]},
        "leaderboard": leaderboard
    })
    return result

@app.post("/api/participants/{bib}/break/end")
async def end_runner_break(bib: int):
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    result = db.end_manual_break(bib)
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "runner_resumed",
        "data": {"bib_number": bib, "name": participant["name"]},
        "leaderboard": leaderboard
    })
    return result

@app.post("/api/participants/{bib}/reset")
async def reset_runner_start(bib: int):
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    db.reset_participant_start(bib)
    await push_leaderboard()
    return {"status": "reset", "bib": bib}

class ManualLapRequest(BaseModel):
    bib_number: int
    lap_duration_seconds: float

class QuickLapAdjustRequest(BaseModel):
    bib_number: Optional[int] = None
    lap_duration_seconds: Optional[float] = None

@app.get("/api/laps/flagged")
async def get_flagged_laps():
    """Returns any laps flagged for admin review."""
    return db.get_flagged_laps()

@app.post("/api/laps/{lap_id}/accept")
async def accept_flagged_lap(lap_id: int):
    """Accepts a flagged lap, removing the warning."""
    db.clear_lap_flag(lap_id)
    await broadcast_ws({"type": "flagged_laps_updated", "flagged": db.get_flagged_laps()})
    return {"status": "accepted"}

@app.post("/api/laps/{lap_id}/split")
async def split_flagged_lap(lap_id: int):
    """Splits a double-duration flagged lap into 2 equal laps."""
    success = db.split_lap(lap_id)
    if not success:
        raise HTTPException(status_code=404, detail="Lap not found")
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "leaderboard_update",
        "leaderboard": leaderboard
    })
    await broadcast_ws({"type": "flagged_laps_updated", "flagged": db.get_flagged_laps()})
    return {"status": "split"}

@app.post("/api/participants/{bib}/quick-add-lap")
async def quick_add_lap(bib: int, req: Optional[QuickLapAdjustRequest] = None):
    """Quickly increments a runner's lap count, estimating split from average or time difference."""
    if db.get_event_state()["status"] == "ended":
        raise EventStateError("This event has ended. Reopen it to edit laps.")
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")

    last_lap = db.get_last_lap(bib)
    lap_number = (last_lap["lap_number"] + 1) if last_lap else 1
    now = time.time()

    # Calculate sensible duration
    last_crossing = participant.get("last_crossing_time") or participant.get("start_time")
    if req and req.lap_duration_seconds and req.lap_duration_seconds > 0:
        duration = req.lap_duration_seconds
    elif last_crossing and (now - last_crossing) > 10.0:
        duration = min(300.0, now - last_crossing)
    elif last_lap and last_lap.get("lap_duration_seconds"):
        duration = last_lap["lap_duration_seconds"]
    else:
        duration = 75.0

    # If participant hasn't started yet, set start time first before updating last_crossing_time
    if not participant.get("start_time"):
        db.set_participant_start(bib, now - duration)

    lap_id = db.record_lap(
        bib_number=bib,
        lap_number=lap_number,
        crossing_time=now,
        lap_duration_seconds=round(duration, 2),
        peak_rssi=0.0,
        manual_entry=True
    )
    db.update_participant_last_crossing(bib, now)

    leaderboard = db.get_leaderboard()
    lap_data = {
        "id": lap_id,
        "bib_number": bib,
        "name": participant["name"],
        "lap_number": lap_number,
        "lap_duration_seconds": round(duration, 2),
        "crossing_time": now,
        "peak_rssi": 0.0,
        "manual_entry": True
    }
    await broadcast_ws({
        "type": "lap_completed",
        "lap": lap_data,
        "leaderboard": leaderboard
    })
    return {"status": "lap_added", "lap": lap_data}

@app.post("/api/participants/{bib}/quick-remove-lap")
async def quick_remove_lap(bib: int):
    """Quickly removes the most recent lap for a runner (undoing false/duplicate crossing)."""
    if db.get_event_state()["status"] == "ended":
        raise EventStateError("This event has ended. Reopen it to edit laps.")
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    removed = db.delete_last_lap_for_runner(bib)
    if not removed:
        raise HTTPException(status_code=400, detail="No laps to remove for this runner")
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "lap_removed",
        "bib_number": bib,
        "removed_lap": removed,
        "leaderboard": leaderboard
    })
    return {"status": "lap_removed", "removed_lap": removed}

@app.post("/api/laps/manual")
async def add_manual_lap(req: ManualLapRequest):
    participant = db.get_participant_by_bib(req.bib_number)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    last_lap = db.get_last_lap(req.bib_number)
    lap_number = (last_lap["lap_number"] + 1) if last_lap else 1
    crossing = time.time()

    lap_id = db.record_lap(
        bib_number=req.bib_number,
        lap_number=lap_number,
        crossing_time=crossing,
        lap_duration_seconds=req.lap_duration_seconds,
        peak_rssi=0.0,
        manual_entry=True
    )
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "lap_completed",
        "lap": {
            "id": lap_id,
            "bib_number": req.bib_number,
            "name": participant["name"],
            "lap_number": lap_number,
            "lap_duration_seconds": req.lap_duration_seconds,
            "crossing_time": crossing
        },
        "leaderboard": leaderboard
    })
    return {"status": "lap_added", "lap_number": lap_number}

@app.delete("/api/laps/{lap_id}")
async def delete_lap(lap_id: int):
    db.delete_lap(lap_id)
    await push_leaderboard()
    return {"status": "lap_deleted"}

# Simulator Controls
@app.post("/api/simulator/trigger/{bib}")
async def trigger_simulated_crossing(bib: int):
    result = await lap_engine.trigger_manual_crossing(bib, force=True)
    if not result:
        raise HTTPException(status_code=404, detail="Runner not found")
    return {"status": "crossing_simulated", "runner": result["name"], "bib": bib}

class SpeedRequest(BaseModel):
    speed: float

@app.post("/api/simulator/speed")
async def set_simulator_speed(req: SpeedRequest):
    if isinstance(reader, MockReader):
        reader.set_speed_multiplier(req.speed)
        lap_engine.speed_multiplier = req.speed
        await broadcast_ws({
            "type": "sim_speed_updated",
            "speed": req.speed
        })
        return {"status": "speed_set", "speed": req.speed}
    raise HTTPException(status_code=400, detail="Hardware is not in mock mode")
