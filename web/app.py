import os
import re
import json
import secrets
import asyncio
import logging
import yaml
from markupsafe import Markup
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core import exports
from core import clock
from core.commentary import CommentaryEngine
from core.db import Database, RegistrationError, EventStateError, LapEditError, is_unclaimed
from core.goals import compute_goal_progress
from core.models import epc_to_apriltag
from core.predict5k import Predict5KManager
from core.registration_import import parse_registration_csv
from core.lap_engine import LapEngine
from hardware.apriltag_reader import (
    APRILTAG_AVAILABLE,
    AprilTagCameraReader,
    list_available_cameras,
)
from hardware.mock_reader import MockReader
from hardware.serial_reader import SerialReader, DEFAULT_REARM_INTERVAL, DEFAULT_SILENCE_REARM
from hardware.frame_capture import open_capture
from web import auth, join
from web.reader_health import ReaderHealth
from web.live_board import CoalescedPush
from web.broadcaster import Broadcaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("RaceApp")
SERVER_BOOT_ID = secrets.token_hex(6)

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

def _forget_ws(ws: WebSocket):
    for conns in (active_connections, admin_connections):
        if ws in conns:
            conns.remove(ws)
    broadcaster.forget(ws)

# Every push goes through per-client queues, so a stalled phone can't hold up the lap engine
# or the other screens (issue #84). A dropped client's socket is closed and its page reconnects.
broadcaster = Broadcaster(on_drop=_forget_ws)

async def broadcast_ws(message: Dict[str, Any], admin_only: bool = False):
    """Queues `message` for every live page (or only admin pages) and returns at once."""
    broadcaster.publish(message, admin_connections if admin_only else active_connections)

async def push_leaderboard():
    full_board.pushed_now()
    await broadcast_ws({"type": "leaderboard_update", "leaderboard": db.get_leaderboard()})

# Full leaderboard at most every FULL_BOARD_INTERVAL s while laps come in (issue #71)
full_board = CoalescedPush(push_leaderboard)

async def broadcast_runner_change(message: Dict[str, Any], bib_number: int):
    """Lap/start/resume: the message carries only this runner's updated row ("row"), which the
    pages merge into their leaderboard; the full board follows within FULL_BOARD_INTERVAL."""
    rows = db.get_leaderboard(bib_number=bib_number)
    message["row"] = rows[0] if rows else None
    await broadcast_ws(message)
    if active_connections:
        full_board.mark_dirty()

# Trackside voice & display commentary engine (non-repeating shuffle deck)
commentary_engine = CommentaryEngine(
    burn_frequency=int(os.getenv("BURN_FREQUENCY", (config.get("event") or {}).get("burn_frequency", 25)))
)

# Callback when a lap is completed
async def on_lap_completed(lap_data: Dict[str, Any]):
    bib = lap_data["bib_number"]
    all_laps = db.get_laps_for_runner(bib)
    if "goal" not in lap_data:
        participant = db.get_participant_by_bib(bib)
        ev = db.get_event_state()
        total_time = sum((l.get("lap_duration_seconds") or 0.0) for l in all_laps)
        lap_data["goal"] = compute_goal_progress(
            participant.get("goal_type") if participant else None,
            participant.get("goal_value") if participant else None,
            total_laps=len(all_laps) or (lap_data.get("lap_number") or 1),
            total_time_seconds=total_time or (lap_data.get("lap_duration_seconds") or 0.0),
            track_length_m=ev.get("track_length_m") or 400.0,
            last_lap_seconds=lap_data.get("lap_duration_seconds"),
        )
    if "commentary" not in lap_data:
        prior = [
            l["lap_duration_seconds"]
            for l in all_laps
            if l["id"] != lap_data.get("id") and l.get("lap_duration_seconds")
        ]
        prev_best = min(prior) if prior else None
        prev_avg = (sum(prior) / len(prior)) if prior else None
        goal_speech = lap_data["goal"]["speech_text"] if lap_data.get("goal") else None
        lap_data["commentary"] = commentary_engine.for_lap(
            name=lap_data.get("name") or f"Bib {bib}",
            lap_number=lap_data.get("lap_number") or 1,
            lap_duration_seconds=lap_data.get("lap_duration_seconds") or 0.0,
            prev_best_seconds=prev_best,
            prev_avg_seconds=prev_avg,
            goal_speech=goal_speech,
        )
    await broadcast_runner_change({"type": "lap_completed", "lap": lap_data}, bib)

# Callback when a runner begins (Chip Start / Lap 0)
async def on_runner_started(start_data: Dict[str, Any]):
    await broadcast_runner_change({"type": "runner_started", "data": start_data}, start_data["bib_number"])

# Callback when a runner resumes after a break
async def on_runner_resumed(resume_data: Dict[str, Any]):
    await broadcast_runner_change({"type": "runner_resumed", "data": resume_data}, resume_data["bib_number"])

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
    snap = reader_health.snapshot(details, engine=lap_engine.flush_health())
    snap["apriltag"] = apriltag_reader.health()
    return snap

async def _every(interval: float, name: str, tick):
    """Runs `tick` every `interval` s for good. A failing tick is logged, never fatal (issue #83)."""
    failures = 0
    while True:
        await asyncio.sleep(interval)
        try:
            await tick()
            failures = 0
        except Exception:
            failures += 1
            if failures == 1 or failures % 100 == 0:
                logger.exception(f"{name} failed ({failures} in a row); retrying")

async def _push_reader_health():
    if admin_connections:
        await broadcast_ws({"type": "reader_health", "health": reader_health_snapshot()}, admin_only=True)

async def reader_health_loop():
    await _every(READER_HEALTH_INTERVAL, "Reader health push", _push_reader_health)

async def tag_event_loop():
    await _every(TAG_EVENT_INTERVAL, "Tag event push", flush_tag_events)

crossing_window = config.get("event", {}).get("crossing_window_seconds", 2.0)
lap_engine = LapEngine(
    db=db,
    crossing_window=crossing_window,
    on_lap_completed=on_lap_completed,
    on_runner_started=on_runner_started,
    on_runner_resumed=on_runner_resumed,
    on_tag_detected=on_tag_detected,
    on_unassigned_tag=on_unassigned_tag,
    auto_register_unassigned=True,
)
# A Pi without an RTC boots with the wrong time. Mid-race, carry on the race's own timeline
# (issue #128); otherwise just never start before what's stored (issue #2).
lap_engine.resume_clock()

lap_engine.refresh_event()

def _on_predict_finished(entry_payload: Dict[str, Any]):
    if main_event_loop is None:
        return
    async def _push():
        await broadcast_ws({
            "type": "predict_finished",
            "entry": entry_payload,
            "state": predict_5k.get_state(),
        })
    asyncio.run_coroutine_threadsafe(_push(), main_event_loop)

predict_5k = Predict5KManager(lambda: db.db_path, on_runner_finished=_on_predict_finished)

def on_reader_tag(tag_read):
    """Called on the reader's thread for every read. Hands it to the event loop, so the lap
    engine's buffers are only ever touched from one thread (issue #13)."""
    reader_health.note_read()
    loop = main_event_loop
    if loop is None:
        return  # reader started before the app; can't happen via lifespan, but never crash it
    try:
        loop.call_soon_threadsafe(lap_engine.process_tag_read, tag_read)
        loop.call_soon_threadsafe(predict_5k.process_tag_read, tag_read)
    except RuntimeError:
        pass  # loop already closed during shutdown

# Optical AprilTag webcam reader (runs alongside RFID for sensor fusion, or standalone when ENABLE_RFID=false)
enable_rfid = _flag("ENABLE_RFID", (config.get("hardware") or {}).get("rfid_enabled"), True)
apriltag_enabled_default = _flag("ENABLE_APRILTAG", (config.get("hardware") or {}).get("apriltag_enabled"), False)
apriltag_camera_index = int(os.getenv("CAMERA_INDEX", (config.get("hardware") or {}).get("camera_index", 0)))
apriltag_reader = AprilTagCameraReader(
    on_tag_read=on_reader_tag,
    camera_index=apriltag_camera_index,
)

if use_mock:
    reader = MockReader(on_tag_read=on_reader_tag, speed_multiplier=5.0)
    lap_engine.speed_multiplier = 5.0
    logger.info("Operating in MOCK reader mode (Simulated runners active at 5.0x).")
elif not enable_rfid:
    reader = apriltag_reader
    apriltag_enabled_default = True
    # Camera crossings don't need a 2.5s RFID antenna fringe window; flush briskly
    lap_engine.crossing_window = min(lap_engine.crossing_window, 0.35)
    logger.info(f"Operating in PRODUCTION CAMERA-ONLY mode (AprilTag webcam index={apriltag_camera_index}, RFID disabled).")
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

async def _push_sim_state():
    if active_connections and isinstance(reader, MockReader):
        await broadcast_ws({
            "type": "sim_state",
            "sim": reader.get_state()
        })

async def sim_broadcast_loop():
    """Streams live simulated track positions to connected browsers in mock mode."""
    await _every(0.2, "Simulator push", _push_sim_state)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()

    # Start engine flush loop
    flush_task = asyncio.create_task(lap_engine.flush_loop())
    sim_task = asyncio.create_task(sim_broadcast_loop()) if isinstance(reader, MockReader) else None
    tag_task = asyncio.create_task(tag_event_loop())
    health_task = asyncio.create_task(reader_health_loop())

    # Start RFID reader (and optional AprilTag webcam reader if configured on)
    reader.start()
    if apriltag_enabled_default and APRILTAG_AVAILABLE and reader is not apriltag_reader:
        apriltag_reader.start()

    yield

    if reader is not apriltag_reader:
        apriltag_reader.stop()
    reader.stop()
    flush_task.cancel()
    tag_task.cancel()
    health_task.cancel()
    full_board.cancel()
    broadcaster.cancel()
    if sim_task:
        sim_task.cancel()
    lap_engine.flush_raw_reads()
    lap_engine.save_clock(force=True)

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

def _require_not_ended(event_id: Optional[int] = None):
    """End freezes the results. Pass a lap's event_id: a stale tab may point at an older event."""
    event = db.get_event(event_id)
    if event and event["status"] == "ended":
        raise EventStateError("This event has ended. Reopen it to edit laps.")

def _lap_for_edit(lap_id: int) -> Dict[str, Any]:
    lap = db.get_lap(lap_id)
    if not lap:
        raise HTTPException(status_code=404, detail="Lap not found")
    _require_not_ended(lap["event_id"])
    return lap

async def _broadcast_manual_lap(lap: Dict[str, Any]):
    lap_data = {k: lap[k] for k in ("id", "bib_number", "name", "lap_number", "lap_duration_seconds", "crossing_time")}
    lap_data.update({"peak_rssi": 0.0, "manual_entry": True})
    await broadcast_ws({"type": "lap_completed", "lap": lap_data, "leaderboard": db.get_leaderboard()})
    return lap_data

async def push_event_status(include_leaderboard: bool = True):
    """Tells the engine and every open page that the event changed."""
    lap_engine.refresh_event()
    message: Dict[str, Any] = {"type": "event_status", "event": db.get_event_state(), "server_now": clock.now()}
    if include_leaderboard:
        message["leaderboard"] = db.get_leaderboard()
    await broadcast_ws(message)

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
            "is_mock": use_mock,
            "camera_running": apriltag_reader.is_running,
        }
    )

@app.get("/predict", response_class=HTMLResponse)
async def predict_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="predict.html",
        context={
            "predict_state": predict_5k.get_state(),
            "is_mock": use_mock,
            "camera_running": apriltag_reader.is_running,
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
    participants = {p["bib_number"]: p["name"] for p in db.get_all_participants() if not is_unclaimed(p)}

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
    try:
        # Initial snapshot. It goes through the same queue as every later push, so it's always
        # first, and nothing below awaits until the socket is on the lists (no push is missed).
        init_payload: Dict[str, Any] = {
            "type": "init",
            "boot_id": SERVER_BOOT_ID,
            "server_now": clock.now(),  # pages re-derive clockOffsetSec (#95)
            "event": db.get_event_state(),
            "leaderboard": db.get_leaderboard()
        }
        if isinstance(reader, MockReader):
            init_payload["sim"] = reader.get_state()
        active_connections.append(websocket)
        if channel == "admin" and admin_auth.is_admin(websocket):
            admin_connections.append(websocket)
        broadcaster.send(websocket, init_payload)
        while True:
            # Keepalive / ping
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
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
    if state["status"] == "paused" and state.get("paused_at"):
        return state["paused_at"] - state["start_time"]   # held, like the pages show it (#91)
    return clock.now() - state["start_time"]

@app.get("/api/event")
async def get_event():
    state = db.get_event_state()
    engine = lap_engine.flush_health()
    body = {**state, "elapsed_seconds": round(_elapsed(state), 1), "total_laps": db.get_total_laps_count(),
            "engine_ok": engine["ok"], "engine_seconds_since_tick": engine["seconds_since_tick"]}
    # The Docker HEALTHCHECK polls this: a stalled lap engine must make it fail (issue #83)
    return body if engine["ok"] else JSONResponse(status_code=503, content=body)

@app.post("/api/event/start")
async def start_event():
    lap_engine.prepare_start()
    state = db.start_event()
    lap_engine.save_clock(force=True)
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
    lap_engine.save_clock(force=True)
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
    max_lap_rest_seconds: Optional[float] = Field(default=None, ge=0)
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

# The CSV routes are plain `def`, so FastAPI runs them in its threadpool: a whole race's
# raw_reads.csv takes seconds on a Pi, and on the event loop it froze every screen and the
# lap engine meanwhile (issue #94). Database opens a connection per call, so this is safe.
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
def export_results(event_id: int):
    return _csv_response(event_id, "results", exports.results_csv)

@app.get("/api/events/{event_id}/laps.csv")
def export_laps(event_id: int):
    return _csv_response(event_id, "laps", exports.laps_csv)

@app.get("/api/events/{event_id}/registrations.csv")
def export_registrations(event_id: int):
    return _csv_response(event_id, "registrations", exports.registrations_csv)

@app.get("/api/events/{event_id}/raw_reads.csv")
def export_raw_reads(event_id: int):
    return _csv_response(event_id, "raw-reads", exports.raw_reads_csv)

# Participants API
class ParticipantRequest(BaseModel):
    bib_number: int = Field(gt=0)
    name: str
    epc: Optional[str] = ""
    notes: Optional[str] = ""
    email: Optional[str] = ""
    apriltag_id: Optional[int] = None
    goal_type: Optional[str] = None
    goal_value: Optional[Any] = None

@app.get("/api/participants")
async def list_participants():
    return db.get_all_participants()

@app.post("/api/participants")
async def add_participant(p: ParticipantRequest):
    db.add_participant(
        p.bib_number, p.name, p.epc, p.notes or "", p.email or "",
        apriltag_id=p.apriltag_id, goal_type=p.goal_type, goal_value=p.goal_value,
    )
    await push_leaderboard()
    return {"status": "created"}

async def _claim_placeholder(bib: int, name: str) -> Dict[str, Any]:
    """Names a runner from the trackside page. A walk-up placeholder isn't timed until it has a
    name, so if the race is on, its clock starts now: they are standing at the mat."""
    before = db.get_participant_by_bib(bib)
    updated = db.rename_participant(bib, name)
    await broadcast_ws({
        "type": "participant_renamed",
        "bib_number": bib,
        "name": updated["name"],
    })
    event_state = db.get_event_state()
    if (is_unclaimed(before) and not updated.get("start_time")
            and event_state.get("status") == "running" and event_state.get("start_time")):
        now = clock.now()
        db.set_participant_start(bib, now, flag_reason=None)
        lap_engine.mark_tag_started_on_mat(updated.get("epc"), now)
        updated = db.get_participant_by_bib(bib)
        await on_runner_started({
            "bib_number": bib,
            "name": updated["name"],
            "start_time": now,
            "event_offset_seconds": round(now - event_state["start_time"], 1),
            "peak_rssi": -45.0,
            "start_flag_reason": None,
        })
    await push_leaderboard()
    return updated

class SelfRegisterRequest(BaseModel):
    name: str
    epc: str
    bib_number: Optional[int] = Field(default=None, gt=0)
    crossing_time: Optional[float] = None
    peak_rssi: Optional[float] = None

@app.post("/api/self-register")
async def self_register_participant(req: SelfRegisterRequest):
    """Registers a runner for a previously unrecognized tag EPC, auto-assigning a bib if omitted.

    When the event is actively running, also records their chip start from the mat crossing (#122)
    so their first completed lap isn't mistaken for Lap 0 (start).
    """
    epc = (req.epc or "").strip()
    if not epc:
        raise RegistrationError("Tag EPC is required")

    existing = db.get_participant_by_epc(epc)
    if is_unclaimed(existing):
        updated = await _claim_placeholder(existing["bib_number"], req.name)
        return {
            "status": "created",
            "started": bool(updated.get("start_time")),
            "start_time": updated.get("start_time"),
            "bib_number": updated["bib_number"],
            "participant": updated,
        }

    bib = req.bib_number
    if bib is None:
        used_bibs = {p["bib_number"] for p in db.get_all_participants()}
        bib = 1
        while bib in used_bibs:
            bib += 1
    db.add_participant(bib, req.name, epc, apriltag_id=epc_to_apriltag(epc))
    participant = db.get_participant_by_bib(bib)

    event_state = db.get_event_state()
    if event_state.get("status") == "running" and event_state.get("start_time"):
        now = clock.now()
        race_start = event_state["start_time"]
        start_t = now if req.crossing_time is None else min(now, max(race_start, req.crossing_time))
        db.set_participant_start(bib, start_t, flag_reason=None)
        lap_engine.mark_tag_started_on_mat(epc, now)
        participant = db.get_participant_by_bib(bib)
        await on_runner_started({
            "bib_number": bib,
            "name": participant["name"],
            "start_time": start_t,
            "event_offset_seconds": round(start_t - race_start, 1),
            "peak_rssi": round(req.peak_rssi, 1) if req.peak_rssi is not None else -45.0,
            "start_flag_reason": None,
        })
        return {
            "status": "created",
            "started": True,
            "start_time": start_t,
            "bib_number": bib,
            "participant": participant,
        }

    await push_leaderboard()
    return {"status": "created", "started": False, "bib_number": bib, "participant": participant}

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
    db.update_participant(
        bib, p.name, p.epc, p.notes or "", p.email,
        apriltag_id=p.apriltag_id, goal_type=p.goal_type, goal_value=p.goal_value,
    )
    await push_leaderboard()
    return {"status": "updated"}

class GoalUpdateRequest(BaseModel):
    goal_type: Optional[str] = None
    goal_value: Optional[Any] = None

@app.patch("/api/participants/{bib}/goal")
async def set_participant_goal_endpoint(bib: int, req: GoalUpdateRequest):
    """Sets or clears a participant's single goal (laps, distance in meters, or time in seconds/MM:SS)."""
    updated = db.set_participant_goal(bib, req.goal_type, req.goal_value)
    await push_leaderboard()
    return {"status": "updated", "bib": bib, "participant": updated}

class AprilTagAssignRequest(BaseModel):
    apriltag_id: Optional[int] = None

@app.patch("/api/participants/{bib}/apriltag")
async def assign_apriltag(bib: int, req: AprilTagAssignRequest):
    """Pairs (or clears) an AprilTag 16h5 ID (0-29) on a runner for optical / fused timing."""
    db.set_participant_apriltag(bib, req.apriltag_id, swap=True)
    await push_leaderboard()
    return {"status": "assigned", "bib": bib, "participant": db.get_participant_by_bib(bib)}

class RenameParticipantRequest(BaseModel):
    name: str

@app.patch("/api/participants/{bib}/name")
async def rename_participant_endpoint(bib: int, req: RenameParticipantRequest):
    """Inline rename from the trackside crossings feed / unclaimed tags list."""
    updated = await _claim_placeholder(bib, req.name)
    return {"status": "renamed", "bib_number": bib, "participant": updated}

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
    _require_not_ended()
    db.delete_participant(bib)
    await push_leaderboard()
    return {"status": "deleted"}

@app.post("/api/participants/{bib}/start")
async def manual_start_runner(bib: int):
    _require_not_ended()
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
    _require_not_ended()
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
    _require_not_ended()
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
    _require_not_ended()
    participant = db.get_participant_by_bib(bib)
    if not participant:
        raise HTTPException(status_code=404, detail="Runner not found")
    db.reset_participant_start(bib)
    lap_engine.forget_tag(participant.get("epc"))  # a tag still on the mat starts afresh (#113)
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
    _lap_for_edit(lap_id)
    db.clear_lap_flag(lap_id)
    await broadcast_ws({"type": "flagged_laps_updated", "flagged": db.get_flagged_laps()})
    return {"status": "accepted"}

@app.post("/api/laps/{lap_id}/split")
async def split_flagged_lap(lap_id: int):
    """Splits a double-duration flagged lap into 2 equal laps."""
    _lap_for_edit(lap_id)
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

# --- possible missed starts (issue #72) ---
@app.get("/api/starts/flagged")
async def get_flagged_starts():
    """Runners first seen a lap or so after the gun: their start may have been missed."""
    return db.get_flagged_starts()

async def _flagged_changed():
    await broadcast_ws({"type": "flagged_laps_updated", "flagged": db.get_flagged_laps()}, admin_only=True)

@app.post("/api/participants/{bib_number}/start-from-gun")
async def start_from_gun(bib_number: int):
    """Their tag wasn't read at the gun: start them at the gun and add the missing first lap."""
    _require_not_ended()
    result = db.start_from_gun(bib_number)
    await push_leaderboard()
    await _flagged_changed()
    return {"status": "started_from_gun", **result}

@app.post("/api/participants/{bib_number}/keep-start")
async def keep_start(bib_number: int):
    """They really did join late: keep their start as recorded."""
    _require_not_ended()
    db.keep_start(bib_number)
    await _flagged_changed()
    return {"status": "kept"}

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
    _lap_for_edit(lap_id)
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

# ----------------- APRILTAG CAMERA & VOICE ANNOUNCER API -----------------
@app.get("/api/cameras")
async def api_list_cameras():
    cameras = await asyncio.to_thread(list_available_cameras) if APRILTAG_AVAILABLE else []
    return {
        "available": APRILTAG_AVAILABLE,
        "cameras": cameras,
        "health": apriltag_reader.health(),
    }

class CameraConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    camera_index: Optional[int] = None
    debug_overlay: Optional[bool] = None
    mirror: Optional[bool] = None
    finish_line_x: Optional[float] = Field(default=None, ge=0.05, le=0.95)

@app.post("/api/camera/config")
async def api_camera_config(req: CameraConfigRequest):
    if req.enabled and not APRILTAG_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="OpenCV is not installed in this environment. Install opencv-python and pupil-apriltags to enable the AprilTag camera.",
        )
    health = await asyncio.to_thread(
        apriltag_reader.set_config,
        enabled=req.enabled,
        camera_index=req.camera_index,
        debug_overlay=req.debug_overlay,
        mirror=req.mirror,
        finish_line_x=req.finish_line_x,
    )
    await broadcast_ws({"type": "camera_status", "camera": health}, admin_only=True)
    return {"status": "ok", "health": health}

@app.get("/api/camera/video_feed")
async def api_camera_video_feed(request: Request):
    boundary = "apriltagframe"

    async def stream():
        last_seq = 0
        while True:
            if await request.is_disconnected():
                break
            seq, jpg = await asyncio.to_thread(apriltag_reader.wait_for_jpeg, last_seq, 0.5)
            if not jpg or seq == last_seq:
                if not apriltag_reader.is_running:
                    break
                continue
            last_seq = seq
            header = (
                f"--{boundary}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpg)}\r\n\r\n"
            ).encode("ascii")
            yield header + jpg + b"\r\n"

    return StreamingResponse(
        stream(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )

@app.get("/api/tags/printable", response_class=HTMLResponse)
async def api_printable_apriltags():
    if not APRILTAG_AVAILABLE:
        return HTMLResponse(
            "<h2>OpenCV not installed</h2><p>Install opencv-python to generate printable AprilTag 16h5 bibs.</p>",
            status_code=503,
        )
    from apriltag_tracker.generate_tags import render_printable_bibs_html
    return HTMLResponse(render_printable_bibs_html())

class AnnouncerConfigRequest(BaseModel):
    burn_frequency: int = Field(ge=0, le=100)

@app.get("/api/announcer/config")
async def api_get_announcer_config():
    return {
        "burn_frequency": commentary_engine.burn_frequency,
        "counts": commentary_engine.counts(),
    }

@app.post("/api/announcer/config")
async def api_set_announcer_config(req: AnnouncerConfigRequest):
    commentary_engine.set_burn_frequency(req.burn_frequency)
    await broadcast_ws(
        {"type": "announcer_config", "burn_frequency": commentary_engine.burn_frequency},
        admin_only=True,
    )
    return {
        "status": "ok",
        "burn_frequency": commentary_engine.burn_frequency,
        "counts": commentary_engine.counts(),
    }

class AnnouncerTestRequest(BaseModel):
    name: str = "Speedy"
    category: str = "burn"

@app.post("/api/announcer/test")
async def api_test_announcer(req: AnnouncerTestRequest):
    return commentary_engine.for_lap(
        name=req.name,
        lap_number=4,
        lap_duration_seconds=84.2,
        prev_best_seconds=86.0 if req.category == "pr" else 78.0,
        prev_avg_seconds=88.0 if req.category == "fast" else 84.0,
        force_category=req.category,
    )

# ----------------- PREDICT 5K API -----------------
class PredictEntryRequest(BaseModel):
    name: str
    predicted_time: Any
    epc: Optional[str] = None
    apriltag_id: Optional[int] = None

class PredictSettingsRequest(BaseModel):
    name: Optional[str] = None
    countdown_seconds: Optional[int] = Field(default=None, ge=0, le=120)
    min_finish_seconds: Optional[int] = Field(default=None, ge=1, le=7200)

class PredictStartRequest(BaseModel):
    countdown_seconds: Optional[int] = Field(default=None, ge=0, le=120)

class PredictNewRequest(BaseModel):
    name: str = "Club Predict 5K"

class PredictUpdateGuessRequest(BaseModel):
    predicted_time: Any

async def _broadcast_predict_state():
    state = predict_5k.get_state()
    await broadcast_ws({"type": "predict_state", "state": state})
    return state

@app.get("/api/predict/state")
async def api_predict_state():
    return predict_5k.get_state()

@app.post("/api/predict/entries")
async def api_predict_add_entry(req: PredictEntryRequest):
    try:
        entry = predict_5k.add_entry(
            name=req.name,
            predicted_time=req.predicted_time,
            epc=req.epc,
            apriltag_id=req.apriltag_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    state = await _broadcast_predict_state()
    return {"status": "created", "entry": entry, "state": state}

@app.delete("/api/predict/entries/{entry_id}")
async def api_predict_delete_entry(entry_id: int):
    if not predict_5k.delete_entry(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    state = await _broadcast_predict_state()
    return {"status": "deleted", "state": state}

@app.patch("/api/predict/entries/{entry_id}/prediction")
async def api_predict_update_prediction(entry_id: int, req: PredictUpdateGuessRequest):
    try:
        updated = predict_5k.update_entry_prediction(entry_id, req.predicted_time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    state = await _broadcast_predict_state()
    return {"status": "updated", "entry": updated, "state": state}

@app.post("/api/predict/settings")
async def api_predict_settings(req: PredictSettingsRequest):
    predict_5k.update_settings(
        name=req.name,
        countdown_seconds=req.countdown_seconds,
        min_finish_seconds=req.min_finish_seconds,
    )
    state = await _broadcast_predict_state()
    return {"status": "updated", "state": state}

@app.post("/api/predict/start")
async def api_predict_start(req: Optional[PredictStartRequest] = None):
    cd = req.countdown_seconds if req else None
    state = predict_5k.start_race(countdown_override=cd)
    await broadcast_ws({"type": "predict_state", "state": state})
    return {"status": "started", "state": state}

@app.post("/api/predict/reset")
async def api_predict_reset():
    predict_5k.reset_race()
    state = await _broadcast_predict_state()
    return {"status": "reset", "state": state}

@app.post("/api/predict/new")
async def api_predict_new(req: PredictNewRequest):
    predict_5k.new_session(req.name)
    state = await _broadcast_predict_state()
    return {"status": "created", "state": state}

@app.post("/api/predict/entries/{entry_id}/finish")
async def api_predict_manual_finish(entry_id: int):
    payload = predict_5k.record_finish(entry_id, source="manual")
    if not payload:
        raise HTTPException(status_code=409, detail="Runner already finished, race not running, or under minimum finish time.")
    state = predict_5k.get_state()
    await broadcast_ws({"type": "predict_finished", "entry": payload, "state": state})
    return {"status": "finished", "entry": payload, "state": state}

@app.post("/api/predict/reveal/start")
async def api_predict_reveal_start():
    try:
        predict_5k.start_reveal()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    state = await _broadcast_predict_state()
    return {"status": "revealing", "state": state}

@app.post("/api/predict/reveal/next")
async def api_predict_reveal_next():
    try:
        predict_5k.reveal_next()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    state = await _broadcast_predict_state()
    return {"status": "revealed", "state": state}

from web.simulator_routes import router as simulator_router  # noqa: E402
app.include_router(simulator_router)
