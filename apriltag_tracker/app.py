"""FastAPI server for the Trackside AprilTag Lap Counter & Live Tracker."""
from __future__ import annotations

import asyncio
import os
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from generate_tags import render_printable_bibs_html
from tracker import SessionTracker, make_placeholder_avatar_png
from vision import VisionEngine

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


class RegisterPayload(BaseModel):
    tag_id: int = Field(..., ge=0, lt=30)
    name: str = Field(..., min_length=1, max_length=80)
    preview_image_base64: Optional[str] = None
    source_tag_id: Optional[int] = Field(default=None, ge=0, lt=30)


class TagIdPayload(BaseModel):
    tag_id: int = Field(..., ge=0, lt=30)


class LapAdjustPayload(BaseModel):
    delta: int = Field(..., ge=-5, le=5)


class EditRunnerPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    new_tag_id: Optional[int] = Field(default=None, ge=0, lt=30)
    preview_image_base64: Optional[str] = None


class ResetPayload(BaseModel):
    clear_runners: bool = False


class ConfigPayload(BaseModel):
    cooldown_seconds: Optional[float] = Field(default=None, ge=5.0, le=120.0)
    finish_line_x: Optional[float] = Field(default=None, ge=0.1, le=0.9)
    burn_frequency: Optional[int] = Field(default=None, ge=0, le=100)
    debug_overlay: Optional[bool] = None
    mirror: Optional[bool] = None
    simulated_mode: Optional[bool] = None


class SimulateScanPayload(BaseModel):
    mode: str = Field(default="unknown")  # "unknown" | "lap" | "pass"
    tag_id: Optional[int] = Field(default=None, ge=0, lt=30)
    split_seconds: Optional[float] = Field(default=None, gt=0.1, le=3600.0)


class WebSocketHub:
    """Broadcasts real-time JSON events to connected Chrome clients."""

    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        if not self.clients:
            return
        stale: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.clients.discard(ws)

    def emit_from_thread(self, message: Dict[str, Any]) -> None:
        loop = self.loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)


def create_app(
    data_dir: Optional[Path] = None,
    force_simulated_camera: Optional[bool] = None,
    start_vision: bool = True,
) -> FastAPI:
    sim_env = os.environ.get("SIMULATED_CAMERA", "").strip().lower() in ("1", "true", "yes")
    use_sim_cam = sim_env if force_simulated_camera is None else force_simulated_camera
    cam_idx = int(os.environ.get("CAMERA_INDEX", "0"))

    tracker = SessionTracker(data_dir=data_dir)
    if use_sim_cam:
        tracker.simulated_mode = True

    hub = WebSocketHub()
    vision = VisionEngine(
        tracker=tracker,
        on_event=hub.emit_from_thread,
        camera_index=cam_idx,
        force_simulated_camera=use_sim_cam,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        if start_vision:
            vision.start()
        try:
            yield
        finally:
            if start_vision:
                vision.stop()
            hub.loop = None

    app = FastAPI(
        title="Trackside AprilTag Lap Counter & Live Tracker",
        lifespan=lifespan,
    )
    app.state.tracker = tracker
    app.state.vision = vision
    app.state.hub = hub

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/faces/{tag_id}.png")
    async def runner_face(tag_id: int) -> Response:
        path = tracker.face_path(tag_id)
        if path.exists():
            return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})
        png = make_placeholder_avatar_png(tag_id)
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/video_feed")
    async def video_feed(request: Request) -> StreamingResponse:
        boundary = "apriltagmjpegboundary"

        async def frame_generator():
            seq = -1
            while True:
                if await request.is_disconnected():
                    break
                next_seq, jpeg = await asyncio.to_thread(vision.wait_for_jpeg, seq, 0.4)
                if jpeg and next_seq != seq:
                    seq = next_seq
                    header = (
                        f"--{boundary}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    ).encode("ascii")
                    yield header + jpeg + b"\r\n"
                else:
                    await asyncio.sleep(0.01)

        return StreamingResponse(
            frame_generator(),
            media_type=f"multipart/x-mixed-replace; boundary={boundary}",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/tags/printable", response_class=HTMLResponse)
    async def printable_tags(per_page: int = Query(default=6)) -> HTMLResponse:
        return HTMLResponse(render_printable_bibs_html(per_page=per_page))

    @app.get("/api/state")
    async def get_state() -> Dict[str, Any]:
        return {
            **tracker.get_state(),
            "camera": {
                "source": vision.camera_source,
                "fps": vision.fps,
            },
        }

    @app.post("/api/register")
    async def register_runner(payload: RegisterPayload) -> Dict[str, Any]:
        preview = payload.preview_image_base64
        if not preview:
            preview = vision.snap_face_base64(payload.tag_id)
        res = tracker.register_runner(
            tag_id=payload.tag_id,
            name=payload.name,
            preview_image_base64=preview,
            source_tag_id=payload.source_tag_id,
        )
        await hub.broadcast(
            {
                "type": "RUNNER_REGISTERED",
                "runner": res["runner"],
                "is_new": res["is_new"],
                "speech_text": res["speech_text"],
            }
        )
        await hub.broadcast({"type": "STATE_SYNC", **tracker.get_state()})
        return res

    @app.post("/api/rescan_tag")
    async def rescan_tag() -> Dict[str, Any]:
        found = vision.rescan_live_tag()
        if found is None:
            raise HTTPException(
                status_code=404,
                detail="No high-confidence AprilTag currently visible in front of the camera.",
            )
        return found

    @app.post("/api/snap_photo")
    async def snap_photo(payload: TagIdPayload) -> Dict[str, Any]:
        if vision.camera_source == "SIMULATOR" and payload.tag_id not in vision._latest_bboxes:
            b64 = vision.render_synthetic_tag_portrait_base64(payload.tag_id)
        else:
            b64 = vision.snap_face_base64(payload.tag_id)
        prompt = tracker.update_pending_preview(payload.tag_id, b64)
        await hub.broadcast({"type": "PHOTO_UPDATED", **prompt})
        return prompt

    @app.post("/api/ignore_tag")
    async def ignore_tag(payload: TagIdPayload) -> Dict[str, Any]:
        tracker.ignore_tag(payload.tag_id)
        state = tracker.get_state()
        await hub.broadcast({"type": "STATE_SYNC", **state})
        return state

    @app.post("/api/unignore_tag")
    async def unignore_tag(payload: TagIdPayload) -> Dict[str, Any]:
        tracker.unignore_tag(payload.tag_id)
        state = tracker.get_state()
        await hub.broadcast({"type": "STATE_SYNC", **state})
        return state

    @app.post("/api/dismiss_prompt")
    async def dismiss_prompt(payload: TagIdPayload) -> Dict[str, Any]:
        tracker.dismiss_pending(payload.tag_id)
        state = tracker.get_state()
        await hub.broadcast({"type": "STATE_SYNC", **state})
        return state

    @app.post("/api/runners/{tag_id}/lap_adjust")
    async def adjust_runner_laps(tag_id: int, payload: LapAdjustPayload) -> Dict[str, Any]:
        updated = tracker.adjust_laps(tag_id, payload.delta)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Runner with tag {tag_id} not found")
        state = tracker.get_state()
        await hub.broadcast({"type": "STATE_SYNC", **state})
        return {"runner": updated, "state": state}

    @app.post("/api/runners/{tag_id}/edit")
    async def edit_runner(tag_id: int, payload: EditRunnerPayload) -> Dict[str, Any]:
        updated = tracker.edit_runner(
            old_tag_id=tag_id,
            name=payload.name,
            new_tag_id=payload.new_tag_id,
            preview_image_base64=payload.preview_image_base64,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Runner with tag {tag_id} not found")
        state = tracker.get_state()
        await hub.broadcast({"type": "STATE_SYNC", **state})
        return {"runner": updated, "state": state}

    @app.delete("/api/runners/{tag_id}")
    async def delete_runner(tag_id: int) -> Dict[str, Any]:
        removed = tracker.delete_runner(tag_id)
        if not removed:
            raise HTTPException(status_code=404, detail=f"Runner with tag {tag_id} not found")
        state = tracker.get_state()
        await hub.broadcast({"type": "STATE_SYNC", **state})
        return {"deleted_tag_id": tag_id, "state": state}

    @app.post("/api/session/{action}")
    async def session_control(
        action: str, payload: Optional[ResetPayload] = None
    ) -> Dict[str, Any]:
        if action == "start":
            tracker.start_session()
        elif action == "pause":
            tracker.pause_session()
        elif action == "reset":
            tracker.reset_session(clear_runners=bool(payload and payload.clear_runners))
        else:
            raise HTTPException(status_code=400, detail=f"Unknown session action: {action}")
        state = tracker.get_state()
        await hub.broadcast({"type": "STATE_SYNC", **state})
        return state

    @app.post("/api/config")
    async def update_config(payload: ConfigPayload) -> Dict[str, Any]:
        updates = payload.model_dump(exclude_none=True)
        state = tracker.update_config(**updates)
        await hub.broadcast({"type": "STATE_SYNC", **state})
        return state

    @app.post("/api/simulate/scan")
    async def simulate_scan(payload: SimulateScanPayload) -> Dict[str, Any]:
        if payload.mode == "unknown":
            tid = payload.tag_id if payload.tag_id is not None else tracker.next_unregistered_tag_id()
            if tid is None:
                raise HTTPException(status_code=409, detail="All 30 tag16h5 IDs (0-29) are in use.")
            if tracker.simulated_mode or vision.camera_source == "SIMULATOR":
                vision.synth_scene.spawn_runner_pass(tid, duration=2.4)
                preview = vision.render_synthetic_tag_portrait_base64(tid)
            else:
                preview = vision.snap_face_base64(tid)
            prompt = tracker.mark_pending_registration(tid, preview)
            if prompt is None:
                prompt = tracker.pending_registration.get(tid) or {
                    "tag_id": tid,
                    "preview_image_base64": preview,
                }
            await hub.broadcast({"type": "REGISTER_PROMPT", **prompt})
            await hub.broadcast({"type": "STATE_SYNC", **tracker.get_state()})
            return {"status": "prompted", "prompt": prompt}

        if payload.mode == "lap":
            board = tracker.leaderboard()
            if payload.tag_id is not None:
                tid = payload.tag_id
            elif board:
                tid = random.choice(board)["tag_id"]
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Register at least one runner first before simulating a lap.",
                )
            if tracker.simulated_mode or vision.camera_source == "SIMULATOR":
                vision.synth_scene.spawn_runner_pass(tid, duration=1.8)
            rec = tracker.runners.get(tid)
            if payload.split_seconds is not None:
                split = payload.split_seconds
            elif rec and rec.avg_lap_time:
                split = round(max(12.0, rec.avg_lap_time * random.uniform(0.84, 1.24)), 1)
            else:
                split = round(random.uniform(64.0, 88.0), 1)

            evt = tracker.record_crossing(tid, force=True, synthetic_split=split)
            if evt is None:
                raise HTTPException(status_code=404, detail=f"Tag {tid} is not registered.")
            await hub.broadcast({"type": "LAP_EVENT", **evt})
            await hub.broadcast({"type": "STATE_SYNC", **tracker.get_state()})
            return {"status": "lap_recorded", "event": evt}

        if payload.mode == "pack":
            board = tracker.leaderboard()
            if len(board) < 2:
                raise HTTPException(
                    status_code=400,
                    detail="Register at least 2 runners to simulate a pack crossing together.",
                )
            pack_size = min(len(board), 3 if len(board) >= 4 else 2)
            chosen = random.sample(board, k=pack_size)
            events = []
            for idx, item in enumerate(chosen):
                tid = item["tag_id"]
                if tracker.simulated_mode or vision.camera_source == "SIMULATOR":
                    vision.synth_scene.spawn_runner_pass(tid, duration=1.9, slot=idx)
                rec = tracker.runners.get(tid)
                base_split = (
                    rec.avg_lap_time * random.uniform(0.88, 1.2)
                    if (rec and rec.avg_lap_time)
                    else random.uniform(68.0, 84.0)
                )
                split = round(max(12.0, base_split + idx * 0.2), 1)
                evt = tracker.record_crossing(tid, force=True, synthetic_split=split)
                if evt is not None:
                    events.append(evt)
                    await hub.broadcast({"type": "LAP_EVENT", **evt})
            await hub.broadcast({"type": "STATE_SYNC", **tracker.get_state()})
            return {"status": "pack_recorded", "events": events}

        if payload.mode == "pass":
            tid = payload.tag_id if payload.tag_id is not None else 0
            tracker.update_config(simulated_mode=True)
            vision.synth_scene.spawn_runner_pass(tid, duration=2.5)
            await hub.broadcast({"type": "STATE_SYNC", **tracker.get_state()})
            return {"status": "pass_spawned", "tag_id": tid}

        raise HTTPException(status_code=400, detail=f"Unsupported simulate mode: {payload.mode}")

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await hub.connect(ws)
        try:
            state = tracker.get_state()
            await ws.send_json({"type": "STATE_SYNC", **state})
            for prompt in state["pending_prompts"]:
                await ws.send_json({"type": "REGISTER_PROMPT", **prompt})

            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")
                if mtype == "SET_CONFIG":
                    updated = tracker.update_config(**msg.get("config", {}))
                    await hub.broadcast({"type": "STATE_SYNC", **updated})
                elif mtype == "SESSION_CONTROL":
                    act = msg.get("action")
                    if act == "start":
                        tracker.start_session()
                    elif act == "pause":
                        tracker.pause_session()
                    elif act == "reset":
                        tracker.reset_session(clear_runners=bool(msg.get("clear_runners")))
                    await hub.broadcast({"type": "STATE_SYNC", **tracker.get_state()})
                elif mtype == "DISMISS_PROMPT":
                    tid = msg.get("tag_id")
                    if tid is not None:
                        tracker.dismiss_pending(int(tid))
                        await hub.broadcast({"type": "STATE_SYNC", **tracker.get_state()})
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(ws)

    return app


app = create_app(start_vision=os.environ.get("APRILTAG_DISABLE_VISION") != "1")
