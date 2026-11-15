from typing import Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from hardware.mock_reader import MockReader, SCENARIO_METADATA, SIMULATOR_PRESETS

router = APIRouter()


class PresetRequest(BaseModel):
    preset: str
    weights: Optional[Dict[str, float]] = None


class ScenarioTriggerRequest(BaseModel):
    scenario: str
    bib: Optional[int] = None


def _get_app_state():
    import web.app as app_module
    reader = app_module.reader
    if isinstance(reader, MockReader):
        if reader.participant_provider is None:
            reader.participant_provider = app_module.db.get_all_participants
        if reader.event_state_provider is None:
            reader.event_state_provider = app_module.db.get_event_state
    return app_module


@router.get("/simulator", response_class=HTMLResponse)
async def view_simulator(request: Request):
    app_module = _get_app_state()
    event = app_module.db.get_event_state()
    leaderboard = app_module.db.get_leaderboard()
    participants = app_module.db.get_all_participants()
    is_mock = isinstance(app_module.reader, MockReader)
    sim_status = app_module.reader.get_status() if is_mock else None
    return app_module.templates.TemplateResponse(
        request,
        "simulator.html",
        {
            "event": event,
            "leaderboard": leaderboard,
            "participants": participants,
            "is_mock": is_mock,
            "sim_status": sim_status,
            "presets": list(SIMULATOR_PRESETS.keys()),
            "scenarios": SCENARIO_METADATA,
        },
    )


@router.get("/api/simulator/status")
async def get_simulator_status():
    app_module = _get_app_state()
    if not isinstance(app_module.reader, MockReader):
        raise HTTPException(status_code=400, detail="Hardware is not in mock mode")
    status = app_module.reader.get_status()
    status["event"] = app_module.db.get_event_state()
    status["leaderboard"] = app_module.db.get_leaderboard()
    status["buffered_epcs"] = {
        epc: len(reads) for epc, reads in list(app_module.lap_engine.buffers.items()) if reads
    }
    return status


@router.post("/api/simulator/preset")
async def update_simulator_preset(req: PresetRequest):
    app_module = _get_app_state()
    if not isinstance(app_module.reader, MockReader):
        raise HTTPException(status_code=400, detail="Hardware is not in mock mode")
    try:
        app_module.reader.set_preset(req.preset, custom_weights=req.weights)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return app_module.reader.get_status()


@router.post("/api/simulator/scenario")
async def trigger_simulator_scenario(req: ScenarioTriggerRequest):
    app_module = _get_app_state()
    if not isinstance(app_module.reader, MockReader):
        raise HTTPException(status_code=400, detail="Hardware is not in mock mode")
    if req.scenario not in SCENARIO_METADATA:
        raise HTTPException(status_code=400, detail=f"Unknown scenario: {req.scenario}")
    try:
        result = app_module.reader.trigger_scenario_async(scenario=req.scenario, bib=req.bib)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result


@router.post("/api/simulator/clear-log")
async def clear_simulator_log():
    app_module = _get_app_state()
    if not isinstance(app_module.reader, MockReader):
        raise HTTPException(status_code=400, detail="Hardware is not in mock mode")
    app_module.reader.clear_log()
    return {"status": "cleared"}
