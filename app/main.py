from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ConfigDict

from .config import MODEL_ID, VOICES, Settings
from .engine import SileroEngine
from .service import SynthesisService
from .store import JobError, ACTIVE
from .text import TextError, normalize, VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("silero_tts_bridge")


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = MODEL_ID
    input: str = Field(min_length=1)
    voice: str = "xenia"
    response_format: Literal["opus", "ogg", "mp3", "wav"] = "mp3"
    speed: float = Field(default=1.0, ge=.25, le=4.0, allow_inf_nan=False)
    text_mode: Literal["auto", "prepared"] = "auto"
    lexicon: dict[str, str] = Field(default_factory=dict, max_length=256)


class Keys:
    def __init__(self, settings):
        self.keys = []
        if settings.api_key:
            self.keys.append(("legacy", settings.api_key))
        if settings.keys_file:
            values = json.loads(settings.keys_file.read_text())
            if not isinstance(values, dict) or not values:
                raise RuntimeError("TTS_KEYS_FILE must contain a non-empty client-to-key JSON object")
            for client, keys in values.items():
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client):
                    raise RuntimeError("Invalid client name in TTS_KEYS_FILE")
                if isinstance(keys, list) and not keys:
                    raise RuntimeError("Each TTS client must have at least one key")
                for key in keys if isinstance(keys, list) else [keys]:
                    if not isinstance(key, str) or len(key) < 24:
                        raise RuntimeError("Client keys must contain at least 24 characters")
                    self.keys.append((client, key))
            if len({key for _, key in self.keys}) != len(self.keys):
                raise RuntimeError("Each TTS client key must be unique")

    def authorize(self, authorization):
        if not self.keys:
            return "local"
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        for owner, key in self.keys:
            if secrets.compare_digest(token.encode(), key.encode()):
                return owner
        raise HTTPException(401, "invalid bearer token")


@asynccontextmanager
async def lifespan(app):
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.keys = Keys(settings)
    service = SynthesisService(SileroEngine(settings), settings)
    app.state.service = service
    try:
        await service.start(warmup=settings.warmup)
        yield
    finally:
        await service.close()


app = FastAPI(title="Silero TTS Bridge", version="0.2.2", lifespan=lifespan)


class BodyLimit:
    """Bound even chunked request bodies before JSON parsing."""
    def __init__(self, application):
        self.app = application

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        maximum = Settings.from_env().max_job_chars * 6 + 256_000
        size = 0
        async def limited():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > maximum:
                raise HTTPException(413, "Request body exceeds the configured text budget")
            return message
        await self.app(scope, limited, send)


app.add_middleware(BodyLimit)


def service(request: Request):
    return request.app.state.service


def owner(request: Request, authorization: Annotated[str | None, Header()] = None):
    return request.app.state.keys.authorize(authorization)


def parameters(payload, request, long=False):
    settings = request.app.state.settings
    if payload.model != MODEL_ID:
        raise JobError("invalid_model", f"model must be {MODEL_ID}")
    if payload.voice not in VOICES:
        raise JobError("invalid_voice", "voice must be one of: " + ", ".join(VOICES))
    if len(payload.input) > (settings.max_job_chars if long else settings.max_input_chars):
        raise JobError("input_limit", "Input exceeds the configured text limit; use the jobs API for long text", 413)
    if not payload.input.strip():
        raise JobError("empty_speech", "Input contains no text")
    # Validate custom pronunciation structure before accepting a durable job.
    if any(not k or len(k) > 200 or not v.strip() or len(v) > 500 for k, v in payload.lexicon.items()):
        raise JobError("invalid_lexicon", "Pronunciations require non-empty source <=200 and spoken value <=500 characters")
    return payload.model_dump(exclude={"input"})


@app.get("/healthz")
async def health(request: Request):
    current = service(request)
    ready = current.engine.ready and current.worker and not current.worker.done()
    counts = current.store.db.execute("SELECT state,count(*) AS count FROM jobs GROUP BY state").fetchall()
    return {"status": "ok" if ready else "starting", "model": MODEL_ID, "voices": VOICES, "threads": current.engine.threads, "normalization": VERSION, "queue": {"active": current.active, "pending": sum(r["count"] for r in counts if r["state"] in ACTIVE), "capacity": current.settings.max_pending}, "limits": {"input_characters": current.settings.max_input_chars, "job_characters": current.settings.max_job_chars}}


@app.post("/v1/audio/normalize")
async def prepare(payload: SpeechRequest, request: Request, client=Depends(owner), offset: int = Query(0, ge=0), limit: int = Query(8000, ge=1, le=8000)):
    parameters(payload, request)
    normalized, report = await asyncio.to_thread(normalize, payload.input, payload.text_mode, payload.lexicon)
    return {"text": normalized[offset:offset+limit], "characters": len(normalized), "next_offset": offset+limit if offset+limit < len(normalized) else None, "normalization": report}


@app.post("/v1/audio/jobs", status_code=202)
async def submit(payload: SpeechRequest, request: Request, client=Depends(owner), idempotency_key: Annotated[str | None, Header()] = None):
    options = parameters(payload, request, long=True)
    current = service(request)
    job = current.submit(client, options, payload.input, current.settings.max_job_output_bytes, idempotency_key)
    return current.store.view(job)


@app.get("/v1/audio/jobs")
async def jobs(request: Request, client=Depends(owner), offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=20)):
    current = service(request)
    rows = current.store.db.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT ? OFFSET ?", (client, limit + 1, offset)).fetchall()
    page, used = [], 100
    for row in rows[:limit]:
        item = current.store.view(dict(row))
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if used + size > 31_000:
            break
        page.append(item)
        used += size
    return {"jobs": page, "next_offset": offset + len(page) if len(rows) > len(page) else None}


@app.get("/v1/audio/jobs/{identifier}")
async def inspect(identifier: str, request: Request, client=Depends(owner)):
    current = service(request)
    return current.store.view(current.store.row(identifier, client))


@app.delete("/v1/audio/jobs/{identifier}")
async def cancel(identifier: str, request: Request, client=Depends(owner), purge: bool = False):
    current = service(request)
    if purge:
        job = current.store.row(identifier, client)
        if job["state"] in ACTIVE or current.is_busy(identifier):
            raise JobError("job_busy", "Cancel and wait for the current fragment to finish before cleaning", 409)
        current.store.expire(identifier)
        return current.store.view(current.store.row(identifier, client))
    return current.store.view(current.cancel(identifier, client))


@app.post("/v1/audio/jobs/{identifier}/resume")
async def resume(identifier: str, request: Request, client=Depends(owner)):
    current = service(request)
    current.store.row(identifier, client)
    if current.is_busy(identifier):
        raise JobError("job_busy", "Wait for the current fragment or encoding to stop before resuming", 409)
    job = current.store.resume(identifier, client)
    current.wake.set()
    return current.store.view(job)


def result_response(current, job):
    if job["state"] == "expired":
        raise JobError("expired", "Audio result expired", 410)
    if job["state"] != "completed":
        raise JobError("not_ready", "Audio job is not completed; inspect its state", 409)
    metadata = json.loads(job["result"])
    path = current.store.directory(job["id"]) / ("result." + metadata["extension"])
    if not path.is_file() or path.stat().st_size != metadata["bytes"]:
        raise JobError("result_unavailable", "Saved audio result is unavailable", 410)
    return FileResponse(path, media_type=metadata["content_type"], filename="speech." + metadata["extension"], headers={"X-TTS-Job-ID": job["id"], "X-Audio-Duration": f"{metadata['duration_seconds']:.3f}", "X-TTS-Model": MODEL_ID, "X-Content-SHA256": metadata["sha256"], "ETag": '"' + metadata["sha256"] + '"', "Cache-Control": "private, no-cache"})


@app.get("/v1/audio/jobs/{identifier}/result")
async def result(identifier: str, request: Request, client=Depends(owner)):
    current = service(request)
    return result_response(current, current.store.row(identifier, client))


@app.get("/v1/audio/jobs/{identifier}/segments")
async def segments(identifier: str, request: Request, client=Depends(owner), offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=30)):
    current = service(request)
    job = current.store.row(identifier, client)
    rows = current.store.db.execute("SELECT n,text,frames FROM chunks WHERE job_id=? ORDER BY n LIMIT ? OFFSET ?", (identifier, limit, offset)).fetchall()
    return {"id": identifier, "segments": [{"index": row["n"], "text": row["text"], "ready": row["frames"] is not None} for row in rows], "next_offset": offset + limit if offset + limit < job["total"] else None}


@app.get("/v1/audio/jobs/{identifier}/source")
@app.get("/v1/audio/jobs/{identifier}/normalized")
async def text_artifact(identifier: str, request: Request, client=Depends(owner)):
    current = service(request)
    job = current.store.row(identifier, client)
    name = "normalized" if request.url.path.endswith("normalized") else "source"
    path = current.store.directory(identifier) / (name + ".txt")
    if job["state"] == "expired":
        raise JobError("expired", "Text artifacts expired", 410)
    if not path.is_file():
        raise JobError("not_ready", "Prepared text is not ready; inspect saved segments", 409)
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name + ".txt", headers={"Cache-Control": "no-store"})


@app.post("/v1/audio/speech")
async def create_speech(payload: SpeechRequest, request: Request, client=Depends(owner), idempotency_key: Annotated[str | None, Header()] = None):
    current = service(request)
    options = parameters(payload, request)
    job = current.submit(client, options, payload.input, current.settings.max_output_bytes, idempotency_key, durable=bool(idempotency_key))
    started = time.monotonic()
    while job["state"] in ACTIVE:
        if await request.is_disconnected():
            if not job["durable"]:
                current.cancel(job["id"], client)
            raise HTTPException(499, "Caller disconnected")
        if time.monotonic() - started > 900:
            if not job["durable"]:
                current.cancel(job["id"], client)
            raise JobError("wait_timeout", "Speech wait timed out; inspect job " + job["id"], 504)
        await asyncio.sleep(.1)
        job = current.store.row(job["id"], client)
    if job["state"] == "failed":
        raise HTTPException(422, {**json.loads(job["error"]), "job_id": job["id"]})
    return result_response(current, job)


@app.exception_handler(JobError)
async def job_error(request, error):
    return JSONResponse({"error": {"code": error.code, "message": str(error)}}, status_code=error.status, headers={"Retry-After": "5"} if error.status == 429 else {})


@app.exception_handler(TextError)
async def text_error(request, error):
    return JSONResponse({"error": {"code": error.code, "message": str(error), "issues": error.issues}}, status_code=422)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    # Pydantic's default response echoes invalid input, potentially an entire book or credential.
    return JSONResponse({"error": {"code": "invalid_request", "fields": [{"location": list(item["loc"]), "message": item["msg"]} for item in error.errors()[:10]]}}, status_code=422)


@app.exception_handler(Exception)
async def unexpected(request, error):
    LOGGER.error("Request failed: error_type=%s", type(error).__name__)
    return JSONResponse({"error": {"code": "internal_error", "message": "Speech request failed"}}, status_code=500)
