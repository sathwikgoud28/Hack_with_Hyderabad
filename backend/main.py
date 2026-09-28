"""FastAPI app. Run: uvicorn backend.main:app --reload"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import IncidentAgent, IncidentLog, incident_to_retain_item
from .config import DATA_DIR, ROOT, settings
from .llm import build_llm
from .memory import build_memory
from .models import FailIn, IncidentIn, ResolveIn

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    getattr(memory, "close", lambda: None)()


app = FastAPI(title="DejaFix - incident response agent with memory", lifespan=lifespan)

def _load_json(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


memory = build_memory()
llm = build_llm()
agent = IncidentAgent(memory, llm, IncidentLog(DATA_DIR / "incidents_log.json"), seed=_load_json("seed_incidents.json"))


def seed_memory(reset: bool = False) -> int:
    if reset:
        memory.reset()
        agent.log.clear()
    incidents = _load_json("seed_incidents.json")
    memory.retain([incident_to_retain_item(i) for i in incidents])
    return len(incidents)


# Handlers are plain `def` so FastAPI runs them in a thread pool; the Hindsight
# sync client runs its own event loop and must not be called from inside one.
@app.get("/api/health")
def health():
    count = memory.count()
    return {
        "memory_backend": memory.name,
        "bank_id": settings.hindsight_bank_id if memory.name == "hindsight" else None,
        "memory_count": count,
        # count() returns None when the Hindsight API can't be reached
        "connected": count is not None,
        "llm": settings.groq_model if llm.available else None,
    }


@app.get("/api/examples")
def examples():
    """Example incidents for the form, plus known service names for suggestions."""
    return {
        "examples": _load_json("demo_alerts.json"),
        "services": sorted({i["service"] for i in _load_json("seed_incidents.json")}),
    }


@app.post("/api/incidents")
def investigate(body: IncidentIn):
    return agent.investigate(body)


@app.get("/api/incidents")
def list_incidents(status: str | None = None):
    items = [agent.view(i) for i in agent.log.items if status is None or i["status"] == status]
    return list(reversed(items))


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str):
    rec = agent.log.get(incident_id)
    if rec is None:
        raise HTTPException(404, f"{incident_id} not found")
    return agent.view(rec)


def _lifecycle(action, incident_id: str, body):
    try:
        return agent.view(action(incident_id, body))
    except KeyError:
        raise HTTPException(404, f"{incident_id} not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        logging.exception("memory write failed")
        raise HTTPException(502, f"Could not save to memory: {exc}")


@app.post("/api/incidents/{incident_id}/fail")
def fail(incident_id: str, body: FailIn):
    """The fix did not work (or was rejected): remember it and investigate again."""
    return _lifecycle(agent.fail, incident_id, body)


@app.post("/api/incidents/{incident_id}/resolve")
def resolve(incident_id: str, body: ResolveIn):
    """The fix worked: Hindsight learns the incident."""
    return _lifecycle(agent.resolve, incident_id, body)


@app.get("/api/history")
def history():
    return agent.history()


@app.get("/api/insights/summary")
def insights_summary():
    """Summary cards, patterns and learning progression, computed from real data (fast, no LLM)."""
    return agent.summary()


@app.get("/api/insights")
def insights():
    return agent.insights()


@app.post("/api/admin/seed")
def admin_seed(reset: bool = False):
    try:
        return {"seeded": seed_memory(reset=reset), "backend": memory.name}
    except Exception as exc:
        logging.exception("seed failed")
        raise HTTPException(502, f"Seeding failed: {exc}")


FRONTEND = ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
