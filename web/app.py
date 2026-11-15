import os
import re
import json
import asyncio
import logging
import yaml
from markupsafe import Markup
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core import exports
from core import clock
from core.db import Database, RegistrationError, EventStateError, LapEditError
from core.registration_import import parse_registration_csv
from core.lap_engine import LapEngine
from hardware.mock_reader import MockReader
from hardware.serial_reader import SerialReader, DEFAULT_REARM_INTERVAL, DEFAULT_SILENCE_REARM
from hardware.frame_capture import open_capture
from web import auth, join
from web.reader_health import ReaderHealth

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

# Admin PIN (issue #3). Env wins over config.yaml; see web/auth.py for what's protected.
admin_auth = auth.AdminAuth(auth.resolve_pin(
    os.getenv("ADMIN_PIN", (config.get("admin") or {}).get("pin")), use_mock))

db_path = os.getenv("DB_PATH", config.get("database", {}).get("path", "data/race.db"))
db = Database(db_path, default_event_name=config.get("event", {}).get("name", "Take Back the Track!"))

# Demo runners match the simulator's tags, so they default to on only in mock mode.
# Never seeded on real hardware unless explicitly asked for.
if _flag("SEED_DEMO", config.get("event", {}).get("seed_demo"), use_mock):
    db.seed_demo_runners()

# Active WebSocket connections
active_connections: List[WebSocket] = []
# Connections from logged-in admin pages that asked for raw tag reads (issue #12)
admin_connections: List[WebSocket] = []

async def broadcast_ws(message: Dict[str, Any], admin_only: bool = False):
    targets = admin_connections if admin_only else active_connections
    disconnected = []
    for ws in list(targets):
        try:
            await ws.send_json(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        _forget_ws(ws)

def _forget_ws(ws: WebSocket):
    for conns in (active_connections, admin_connections):
        if ws in conns:
            conns.remove(ws)

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

# Callback when an unrecognized/unassigned tag crosses the mat (issue #45)
async def on_unassigned_tag(tag_data: Dict[str, Any]):
    # Carries the tag ID: admin pages only (#52); /crossings subscribes to the admin channel
    await broadcast_ws({
        "type": "unassigned_tag_detected",
        "data": tag_data,
    }, admin_only=True)

# Callback when any tag is detected (used for Admin auto-fill / registration)
# Raw tag reads arrive 5-20 per crossing per runner. Only the admin and Prepare Bibs pages use
# them, so they go only to admin connections, combined per tag: one message per tag every
# TAG_EVENT_INTERVAL seconds carrying the strongest RSSI seen (issue #12).
TAG_EVENT_INTERVAL = 0.25
pending_tags: Dict[str, Dict[str, Any]] = {}

def _note_tag(tag_info: Dict[str, Any]):
    """Runs on the event loop. Keeps the strongest read per tag until the next flush."""
    current = pending_tags.get(tag_info["epc"])
    if current is None or tag_info["rssi"] > current["rssi"]:
        pending_tags[tag_info["epc"]] = dict(tag_info)

def on_tag_detected(tag_info: Dict[str, Any]):
    """Called on the reader thread for every read."""
    if main_event_loop is None:
        return
    main_event_loop.call_soon_threadsafe(_note_tag, tag_info)

async def flush_tag_events():
    if not pending_tags:
        return
    batch = list(pending_tags.values())
    pending_tags.clear()
    if not admin_connections:
        return
    for tag in batch:
        await broadcast_ws({"type": "tag_detected", "data": tag}, admin_only=True)

# Reader health strip on the admin page (issue #48)
READER_HEALTH_INTERVAL = 2.0
reader_health = ReaderHealth()

def reader_health_snapshot() -> Dict[str, Any]:
    # The strip must never break the admin page: report a failing health() as a problem
    try:
        details = reader.health()
        if not isinstance(details, dict):
            raise TypeError(f"health() returned {type(details).__name__}")
    except Exception as e:
        logger.warning(f"Reader health unavailable: {e}")
        details = {"kind": "unknown", "running": True, "connected": False, "last_error": f"health check failed: {e}"}
    return reader_health.snapshot(details)

async def reader_health_loop():
    while True:
        await asyncio.sleep(READER_HEALTH_INTERVAL)
        if admin_connections:
            await broadcast_ws({"type": "reader_health", "health": reader_health_snapshot()}, admin_only=True)

async def tag_event_loop():
    while True:
        await asyncio.sleep(TAG_EVENT_INTERVAL)
        await flush_tag_events()

crossing_window = config.get("event", {}).get("crossing_window_seconds", 2.0)
lap_engine = LapEngine(
    db=db,
    crossing_window=crossing_window,
    on_lap_completed=on_lap_completed,
    on_runner_started=on_runner_started,
    on_runner_resumed=on_runner_resumed,
    on_tag_detected=on_tag_detected,
    on_unassigned_tag=on_unassigned_tag
)
# A Pi without an RTC can boot with an older time than what's already stored (issue #2)
clock.clock.ensure_not_before(db.latest_timestamp())

lap_engine.refresh_event()

def on_reader_tag(tag_read):
    """Called on the reader's thread for every read. Hands it to the event loop, so the lap
    engine's buffers are only ever touched from one thread (issue #13)."""
    reader_health.note_read()
    loop = main_event_loop
    if loop is None:
        return  # reader started before the app; can't happen via lifespan, but never crash it
    try:
        loop.call_soon_threadsafe(lap_engine.process_tag_read, tag_read)
    except RuntimeError:
        pass  # loop already closed during shutdown

if use_mock:
    reader = MockReader(on_tag_read=on_reader_tag, speed_multiplier=5.0)
    lap_engine.speed_multiplier = 5.0
    logger.info("Operating in MOCK reader mode (Simulated runners active at 5.0x).")
else:
    hw = config.get("hardware") or {}

    def _seconds(key: str, default: float) -> float:
        value = hw.get(key)
        return default if value is None else float(value)  # 0 turns that re-arm off

    reader = SerialReader(
        port=serial_port, baud_rate=baud_rate, on_tag_read=on_reader_tag,
        rearm_interval=_seconds("rearm_interval_seconds", DEFAULT_REARM_INTERVAL),
        silence_rearm=_seconds("silence_rearm_seconds", DEFAULT_SILENCE_REARM),
        # Off unless a path is set; RAW_CAPTURE_PATH wins over config.yaml (issue #51)
        capture=open_capture(os.getenv("RAW_CAPTURE_PATH") or hw.get("raw_capture_path"),
                             hw.get("raw_capture_max_mb")),
    )
    logger.info(f"Operating in PHYSICAL reader mode on {serial_port}.")

main_event_loop = None

async def sim_broadcast_loop():
    """Streams live simulated track positions to connected browsers in mock mode."""
    while True:
        await asyncio.sleep(0.2)
        if active_connections and isinstance(reader, MockReader):
            await broadcast_ws({
                "type": "sim_state",
                "sim": reader.get_state()
            })

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()

    # Start engine flush loop
    flush_task = asyncio.create_task(lap_engine.flush_loop())
    sim_task = asyncio.create_task(sim_broadcast_loop()) if isinstance(reader, MockReader) else None
    tag_task = asyncio.create_task(tag_event_loop())
    health_task = asyncio.create_task(reader_health_loop())

    # Start RFID reader
    reader.start()

    yield

    reader.stop()
    flush_task.cancel()
    tag_task.cancel()
    health_task.cancel()
    if sim_task:
        sim_task.cancel()
    lap_engine.flush_raw_reads()

app = FastAPI(title="Race Timing System", lifespan=lifespan)
# Looked up per request so tests can swap admin_auth.
app.middleware("http")(auth.make_middleware(lambda: admin_auth))

# Templates and static setup
def _tojson(obj) -> Markup:
    """JSON that's safe to drop inside a <script> tag (names come from registrations)."""
    text = json.dumps(obj)
    for char, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(char, escaped)
    return Markup(text)

templates = Jinja2Templates(directory="web/templates")
templates.env.filters["tojson"] = _tojson
# Pages compare their own clock with this to show elapsed race time correctly (issue #2)
templates.env.globals["server_now"] = lambda: f"{clock.now():.3f}"
os.makedirs("web/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="web/static"), name="static")

@app.exception_handler(RegistrationError)
async def registration_error_handler(request: Request, exc: RegistrationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})

@app.exception_handler(EventStateError)
async def event_state_error_handler(request: Request, exc: EventStateError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})

@app.exception_handler(LapEditError)
async def lap_edit_error_handler(request: Request, exc: LapEditError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})

def _require_not_ended():
    if db.get_event_state()["status"] == "ended":
        raise EventStateError("This event has ended. Reopen it to edit laps.")

async def _broadcast_manual_lap(lap: Dict[str, Any]):
    lap_data = {k: lap[k] for k in ("id", "bib_number", "name", "lap_number", "lap_duration_seconds", "crossing_time")}
    lap_data.update({"peak_rssi": 0.0, "manual_entry": True})
    await broadcast_ws({"type": "lap_completed", "lap": lap_data, "leaderboard": db.get_leaderboard()})
    return lap_data

async def push_event_status(include_leaderboard: bool = True):
    """Tells the engine and every open page that the event changed."""
    lap_engine.refresh_event()
    message: Dict[str, Any] = {"type": "event_status", "event": db.get_event_state()}
    if include_leaderboard:
        message["leaderboard"] = db.get_leaderboard()
    await broadcast_ws(message)

async def push_leaderboard():
    await broadcast_ws({"type": "leaderboard_update", "leaderboard": db.get_leaderboard()})

# ----------------- LOGIN -----------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/admin"):
    if admin_auth.is_admin(request):
        return RedirectResponse(url=auth.safe_next(next), status_code=303)
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"next": auth.safe_next(next), "error": None})

@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, pin: str = Form(""), next: str = Form("/admin")):
    client = request.client.host if request.client else "unknown"
    target = auth.safe_next(next)
    if not admin_auth.enabled:
        return RedirectResponse(url=target, status_code=303)
    if admin_auth.check_pin(client, pin):
        res = RedirectResponse(url=target, status_code=303)
        res.set_cookie(auth.COOKIE_NAME, admin_auth.session_token(), max_age=auth.COOKIE_MAX_AGE,
                       httponly=True, samesite="lax")
        return res
    error = ("Too many wrong PINs. Wait a minute and try again." if admin_auth.locked_out(client)
             else "Wrong PIN.")
    return templates.TemplateResponse(request=request, name="login.html", status_code=401,
                                      context={"next": target, "error": error})

@app.post("/logout")
async def logout():
    res = RedirectResponse(url="/leaderboard", status_code=303)
    res.delete_cookie(auth.COOKIE_NAME)
    return res

# ----------------- PAGE ROUTES -----------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # http://race.lan is what people type on the race Wi-Fi: spectators get the public
    # leaderboard, a logged-in admin gets the trackside screen (issue #64)
    return RedirectResponse(url="/crossings" if admin_auth.is_admin(request) else "/leaderboard")

@app.get("/crossings", response_class=HTMLResponse)
async def crossings_page(request: Request):
    event_state = db.get_event_state()
    leaderboard = db.get_leaderboard()
    recent_crossings = db.get_recent_crossings(10)
    return templates.TemplateResponse(
        request=request,
        name="crossings.html",
        context={
            "event": event_state,
            "leaderboard": leaderboard,
            "recent_crossings": recent_crossings,
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
            "is_mock": use_mock,
            "is_admin": admin_auth.is_admin(request),  # crew-only links
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
            "raw_read_count": db.count_raw_reads(),
            "auth_enabled": admin_auth.enabled,
            "reader_health": reader_health_snapshot()
        }
    )

@app.get("/join", response_class=HTMLResponse)
async def join_page(request: Request):
    """Printable poster: QR to join the race Wi-Fi, QR to open the leaderboard (issue #42).
    Admin-only because it shows the Wi-Fi password (which is meant to be shared, on paper)."""
    hotspot = join.hotspot_settings()
    url = join.site_url(hotspot["hostname"], str(request.base_url))
    wifi_qr = join.qr_svg(join.wifi_qr_payload(hotspot["ssid"], hotspot["password"])) if hotspot["ssid"] else None
    return templates.TemplateResponse(request=request, name="join.html", context={
        "event": db.get_event_state(),
        "hotspot": hotspot,
        "url": url,
        "wifi_qr": wifi_qr,
        "url_qr": join.qr_svg(url + "/leaderboard"),
    })

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
            "event": event_state,
            "is_admin": admin_auth.is_admin(request)
        }
    )

# ----------------- WEBSOCKET ROUTE -----------------
@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket, channel: str = "public"):
    """Live updates. `?channel=admin` from a logged-in admin also gets raw tag reads."""
    await websocket.accept()
    active_connections.append(websocket)
    if channel == "admin" and admin_auth.is_admin(websocket):
        admin_connections.append(websocket)
    try:
        # Send initial snapshot
        init_payload: Dict[str, Any] = {
            "type": "init",
            "event": db.get_event_state(),
            "leaderboard": db.get_leaderboard()
        }
        if isinstance(reader, MockReader):
            init_payload["sim"] = reader.get_state()
        await websocket.send_json(init_payload)
        while True:
            # Keepalive / ping
            await websocket.receive_text()
    except WebSocketDisconnect:
        _forget_ws(websocket)

# ----------------- REST API ROUTES -----------------
@app.get("/api/reader/health")
async def get_reader_health():
    """Admin-only (not in auth.PUBLIC_ROUTES): connection state, reads/s, last tag (issue #48)."""
    return reader_health_snapshot()

@app.get("/api/leaderboard")
async def get_leaderboard():
    return db.get_leaderboard()

def _elapsed(state: Dict[str, Any]) -> float:
    if not state["start_time"]:
        return 0.0
    if state["status"] == "ended" and state["end_time"]:
        return state["end_time"] - state["start_time"]
    return clock.now() - state["start_time"]

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

@app.post("/api/event/reset-all")
async def reset_all_event_data():
    """Completely clears laps, breaks, raw reads, and registered runners for the current event after taking a backup."""
    if db.get_event_state()["status"] == "running":
        raise EventStateError("Pause or end the event before resetting everything.")
    lap_engine.buffers.clear()
    lap_engine.flush_raw_reads()
    backup_path = db.backup("pre-full-reset")
    db.reset_all_event_data()
    await push_event_status()
    await push_leaderboard()
    return {"status": "reset_all", "backup": backup_path}

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

class SelfRegisterRequest(BaseModel):
    name: str
    epc: str
    bib_number: Optional[int] = Field(default=None, gt=0)

@app.post("/api/self-register")
async def self_register_participant(req: SelfRegisterRequest):
    """Registers a runner for a previously unrecognized tag EPC, auto-assigning a bib if omitted."""
    epc = (req.epc or "").strip()
    if not epc:
        raise RegistrationError("Tag EPC is required")
    bib = req.bib_number
    if bib is None:
        used_bibs = {p["bib_number"] for p in db.get_all_participants()}
        bib = 1
        while bib in used_bibs:
            bib += 1
    db.add_participant(bib, req.name, epc)
    await push_leaderboard()
    return {"status": "created", "bib_number": bib, "participant": db.get_participant_by_bib(bib)}

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
    now = clock.now()
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
    lap_duration_seconds: Optional[float] = None  # None: the lap ends now

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
    """+1 lap for a runner the reader missed, timed from their previous crossing (issue #9).

    A runner who hasn't started gets a start instead: there's no previous crossing to time a
    lap from, and making one up would put a fake lap in the results.
    """
    _require_not_ended()
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")

    if not (participant.get("last_crossing_time") or participant.get("start_time")):
        now = clock.now()
        db.set_participant_start(bib, now)
        await broadcast_ws({"type": "runner_started",
                            "data": {"bib_number": bib, "name": participant["name"], "start_time": now},
                            "leaderboard": db.get_leaderboard()})
        return {"status": "started", "start_time": now}

    duration = req.lap_duration_seconds if req and req.lap_duration_seconds else None
    lap = db.add_manual_lap(bib, duration=duration, min_gap=db.get_event_state()["min_lap_seconds"])
    return {"status": "lap_added", "lap": await _broadcast_manual_lap(lap)}

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
    """A lap the reader missed: ends `lap_duration_seconds` after the runner's previous crossing,
    or now if no duration is given. The next automatic lap is timed from it (issue #9)."""
    _require_not_ended()
    if not db.get_participant_by_bib(req.bib_number):
        raise HTTPException(status_code=404, detail="Runner not found")
    lap = db.add_manual_lap(req.bib_number, duration=req.lap_duration_seconds)
    lap_data = await _broadcast_manual_lap(lap)
    return {"status": "lap_added", "lap_number": lap["lap_number"], "lap": lap_data}

@app.delete("/api/laps/{lap_id}")
async def delete_lap(lap_id: int):
    """Deletes a lap; its time goes to the next lap (or break, or the lap in progress)."""
    if not db.delete_lap(lap_id):
        raise HTTPException(status_code=404, detail="Lap not found")
    await push_leaderboard()
    return {"status": "lap_deleted"}

# Simulator Controls
@app.post("/api/simulator/trigger/{bib}")
async def trigger_simulated_crossing(bib: int):
    if not isinstance(reader, MockReader):
        raise HTTPException(status_code=400, detail="Hardware is not in mock mode")
    result = await lap_engine.trigger_manual_crossing(bib, force=True)
    if not result:
        raise HTTPException(status_code=404, detail="Runner not found")
    name = result["participant"]["name"]
    outcome = result["outcome"]
    if outcome == "debounced":
        raise HTTPException(
            status_code=409,
            detail=f"Too soon since #{bib}'s last crossing; no lap recorded",
        )
    if outcome is None:
        raise HTTPException(status_code=409, detail="Event is not running; crossing ignored")
    return {"status": "crossing_simulated", "outcome": outcome, "runner": name, "bib": bib}

class SpeedRequest(BaseModel):
    speed: float

@app.get("/api/simulator/state")
async def get_simulator_state():
    if isinstance(reader, MockReader):
        return reader.get_state()
    raise HTTPException(status_code=400, detail="Hardware is not in mock mode")

@app.post("/api/simulator/play")
async def play_simulator():
    if isinstance(reader, MockReader):
        reader.set_paused(False)
        sim = reader.get_state()
        await broadcast_ws({"type": "sim_state", "sim": sim})
        return {"status": "playing", "sim": sim}
    raise HTTPException(status_code=400, detail="Hardware is not in mock mode")

@app.post("/api/simulator/pause")
async def pause_simulator():
    if isinstance(reader, MockReader):
        reader.set_paused(True)
        sim = reader.get_state()
        await broadcast_ws({"type": "sim_state", "sim": sim})
        return {"status": "paused", "sim": sim}
    raise HTTPException(status_code=400, detail="Hardware is not in mock mode")

@app.post("/api/simulator/speed")
async def set_simulator_speed(req: SpeedRequest):
    if isinstance(reader, MockReader):
        reader.set_speed_multiplier(req.speed)
        lap_engine.speed_multiplier = reader.speed_multiplier
        sim = reader.get_state()
        await broadcast_ws({
            "type": "sim_speed_updated",
            "speed": reader.speed_multiplier
        })
        await broadcast_ws({
            "type": "sim_state",
            "sim": sim
        })
        return {"status": "speed_set", "speed": reader.speed_multiplier, "sim": sim}
    raise HTTPException(status_code=400, detail="Hardware is not in mock mode")

from web.simulator_routes import router as simulator_router  # noqa: E402
app.include_router(simulator_router)
