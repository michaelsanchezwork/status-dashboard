from fastapi import FastAPI
from fastapi import Body
import json
from datetime import datetime
import os, time, platform, logging
import psutil
from collections import deque
from pathlib import Path
import uuid
from fastapi import HTTPException
import os
from fastapi import Header, HTTPException, Depends
import logging
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

try:
    import docker
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False

LOG_PATH = os.getenv("LOG_PATH", "/var/log/app/app.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = FastAPI(title="Status Dashboard")

REQUEST_COUNT = Counter(
    "app_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "app_request_latency_seconds",
    "Request latency in seconds",
    ["method", "path"],
)

logger = logging.getLogger("uvicorn.error")

@app.middleware("http")
async def log_requests(request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start
    ms = int(elapsed * 1000)

    method = request.method
    path = request.url.path
    status = str(response.status_code)

    # Metrics
    REQUEST_COUNT.labels(method=method, path=path, status=status).inc()
    REQUEST_LATENCY.labels(method=method, path=path).observe(elapsed)

    # Your log line (keep whatever you like)
    logger.info("REQ %s %s -> %s (%dms)", method, path, response.status_code, ms)
    return response

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

START_TIME = time.time()
DATA_DIR = Path("/var/app/data")
NOTES_FILE = DATA_DIR / "notes.json"
API_KEY = os.getenv("API_KEY", "")

def require_api_key(x_api_key: str | None = Header(default=None)):
    if not API_KEY:
        # If you forget to set it, fail closed (safer)
        raise HTTPException(status_code=500, detail="API key not configured")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

def _load_notes():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not NOTES_FILE.exists():
        NOTES_FILE.write_text("[]", encoding="utf-8")

    notes = json.loads(NOTES_FILE.read_text(encoding="utf-8"))

    # Backfill IDs for older notes (so delete can work)
    changed = False
    for n in notes:
        if "id" not in n:
            n["id"] = uuid.uuid4().hex
            changed = True

    if changed:
        _save_notes(notes)

    return notes

def _save_notes(notes):
    NOTES_FILE.write_text(json.dumps(notes, indent=2), encoding="utf-8")

@app.get("/notes")
def get_notes():
    return _load_notes()

@app.post("/notes", status_code=201)
def add_note(payload: dict = Body(...), _: None = Depends(require_api_key)):
    text = (payload.get("text") or "").strip()
    if not text:
        return {"error": "text is required"}

    notes = _load_notes()
    notes.append({
        "id": uuid.uuid4().hex,
        "text": text,
        "created": datetime.utcnow().isoformat() + "Z"
    })
    _save_notes(notes)
    return {"ok": True}

@app.get("/health")
def health():
    logging.info("GET /health")
    return {
        "status": "ok",
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "uptime_seconds": int(time.time() - START_TIME),
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage('/').percent
    }
@app.get("/logs")
def get_logs(lines: int = 100):
    """
    Return the last N lines of the app log file.
    Usage: /logs?lines=200
    """
    log_path = Path(LOG_PATH)

    if not log_path.exists():
        return {"status": "not_found", "path": str(log_path)}

    # Keep it safe: cap max lines so you don't accidentally dump huge logs
    lines = max(1, min(lines, 500))

    with log_path.open("r", errors="ignore") as f:
        tail = list(deque(f, maxlen=lines))

    return {
        "status": "ok",
        "path": str(log_path),
        "lines": lines,
        "tail": [line.rstrip("\n") for line in tail],
    }

@app.get("/docker")
def docker_status():
    logging.info("GET /docker")
    if not DOCKER_AVAILABLE:
        return {"status": "unavailable", "reason": "python docker SDK not installed in container"}

    client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    containers = client.containers.list()

    return {
        "status": "ok",
        "running": [
            {"name": c.name, "id": c.short_id, "status": c.status}
            for c in containers
        ]
    }

@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: str, _: None = Depends(require_api_key)):
    notes = _load_notes()
    new_notes = [n for n in notes if n.get("id") != note_id]

    if len(new_notes) == len(notes):
        raise HTTPException(status_code=404, detail="Note not found")

    _save_notes(new_notes)
    return



