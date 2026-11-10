import os
import time
import json
import asyncio
import logging
import yaml
from markupsafe import Markup
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.db import Database
from core.models import Participant, Lap, TagRead
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

db_path = os.getenv("DB_PATH", config.get("database", {}).get("path", "data/race.db"))
db = Database(db_path)

# Ensure demo runners exist for initial testing
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

# Hardware setup (Mock vs Serial)
use_mock_env = os.getenv("MOCK_HARDWARE")
if use_mock_env is not None:
    use_mock = use_mock_env.lower() in ("true", "1", "yes")
else:
    use_mock = config.get("hardware", {}).get("mock", True)

serial_port = os.getenv("SERIAL_PORT", config.get("hardware", {}).get("serial_port", "/dev/ttyUSB0"))
baud_rate = int(os.getenv("BAUD_RATE", config.get("hardware", {}).get("baud_rate", 115200)))

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

app = FastAPI(title="Race Timing System", lifespan=lifespan)

# Templates and static setup
templates = Jinja2Templates(directory="web/templates")
templates.env.filters["tojson"] = lambda obj: Markup(json.dumps(obj))
os.makedirs("web/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="web/static"), name="static")

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
    event_state = db.get_event_state()
    leaderboard = db.get_leaderboard()
    return templates.TemplateResponse(
        request=request,
        name="leaderboard.html",
        context={
            "event": event_state,
            "leaderboard": leaderboard,
            "is_mock": use_mock
        }
    )

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    event_state = db.get_event_state()
    participants = db.get_all_participants()
    leaderboard = db.get_leaderboard()
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "event": event_state,
            "participants": participants,
            "leaderboard": leaderboard,
            "is_mock": use_mock
        }
    )

@app.get("/runner/{bib}", response_class=HTMLResponse)
async def runner_page(request: Request, bib: int):
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    laps = db.get_laps_for_runner(bib)
    breaks = db.get_breaks_for_runner(bib)
    event_state = db.get_event_state()
    return templates.TemplateResponse(
        request=request,
        name="runner_detail.html",
        context={
            "runner": participant,
            "laps": laps,
            "breaks": breaks,
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
        event_state = db.get_event_state()
        leaderboard = db.get_leaderboard()
        await websocket.send_json({
            "type": "init",
            "event": event_state,
            "leaderboard": leaderboard
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

@app.get("/api/event")
async def get_event():
    state = db.get_event_state()
    elapsed = 0.0
    if state["is_active"] and state["start_time"]:
        elapsed = time.time() - state["start_time"]
    return {**state, "elapsed_seconds": round(elapsed, 1), "total_laps": db.get_total_laps_count()}

@app.post("/api/event/start")
async def start_event():
    state = db.get_event_state()
    start_time = state["start_time"] if state["start_time"] else time.time()
    db.update_event_state(is_active=True, start_time=start_time)
    updated = db.get_event_state()
    await broadcast_ws({"type": "event_status", "event": updated})
    return {"status": "started", "start_time": start_time}

@app.post("/api/event/pause")
async def pause_event():
    db.update_event_state(is_active=False)
    updated = db.get_event_state()
    await broadcast_ws({"type": "event_status", "event": updated})
    return {"status": "paused"}

@app.post("/api/event/reset")
async def reset_event():
    db.reset_race_data()
    updated = db.get_event_state()
    leaderboard = db.get_leaderboard()
    await broadcast_ws({"type": "event_status", "event": updated, "leaderboard": leaderboard})
    return {"status": "reset"}

class SettingsRequest(BaseModel):
    name: Optional[str] = None
    min_lap_seconds: Optional[float] = None
    max_lap_rest_seconds: Optional[float] = None

@app.post("/api/event/settings")
async def update_settings(req: SettingsRequest):
    db.update_event_state(
        name=req.name, 
        min_lap_seconds=req.min_lap_seconds,
        max_lap_rest_seconds=req.max_lap_rest_seconds
    )
    updated = db.get_event_state()
    await broadcast_ws({"type": "event_status", "event": updated})
    return {"status": "updated", "event": updated}

# Participants API
class ParticipantRequest(BaseModel):
    bib_number: int
    name: str
    epc: str
    notes: Optional[str] = ""

@app.post("/api/participants")
async def add_participant(p: ParticipantRequest):
    success = db.add_participant(p.bib_number, p.name, p.epc, p.notes)
    if not success:
        raise HTTPException(status_code=400, detail="Bib number or EPC already exists")
    await broadcast_ws({"type": "leaderboard_update", "leaderboard": db.get_leaderboard()})
    return {"status": "created"}

@app.put("/api/participants/{bib}")
async def edit_participant(bib: int, p: ParticipantRequest):
    success = db.update_participant(bib, p.name, p.epc, p.notes)
    if not success:
        raise HTTPException(status_code=400, detail="Update failed")
    await broadcast_ws({"type": "leaderboard_update", "leaderboard": db.get_leaderboard()})
    return {"status": "updated"}

@app.delete("/api/participants/{bib}")
async def remove_participant(bib: int):
    db.delete_participant(bib)
    await broadcast_ws({"type": "leaderboard_update", "leaderboard": db.get_leaderboard()})
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
    leaderboard = db.get_leaderboard()
    await broadcast_ws({
        "type": "leaderboard_update",
        "leaderboard": leaderboard
    })
    return {"status": "reset", "bib": bib}

class ManualLapRequest(BaseModel):
    bib_number: int
    lap_duration_seconds: float

class QuickLapAdjustRequest(BaseModel):
    bib_number: int
    lap_duration_seconds: Optional[float] = None

@app.post("/api/participants/{bib}/quick-add-lap")
async def quick_add_lap(bib: int, req: Optional[QuickLapAdjustRequest] = None):
    """Quickly increments a runner's lap count, estimating split from average or time difference."""
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
        
    lap_id = db.record_lap(
        bib_number=bib,
        lap_number=lap_number,
        crossing_time=now,
        lap_duration_seconds=round(duration, 2),
        peak_rssi=0.0,
        manual_entry=True
    )
    db.update_participant_last_crossing(bib, now)
    
    # If participant hasn't started yet, set start time
    if not participant.get("start_time"):
        db.set_participant_start(bib, now - duration)
        
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
    await broadcast_ws({"type": "leaderboard_update", "leaderboard": db.get_leaderboard()})
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
