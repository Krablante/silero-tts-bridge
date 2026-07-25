from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .config import MODEL_ALIASES, MODEL_ID, VOICES, Settings
from .engine import SileroEngine, SynthesisError
from .service import QueueFullError, SynthesisService


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("silero_tts_bridge")


class SpeechRequest(BaseModel):
    model: str = MODEL_ID
    input: str = Field(min_length=1)
    voice: str = "xenia"
    response_format: str = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    service = SynthesisService(SileroEngine(settings), settings.max_pending)
    app.state.settings = settings
    app.state.service = service
    LOGGER.info("Loading Silero model")
    await service.start(warmup=settings.warmup)
    LOGGER.info("Service ready: model=%s voices=%s", MODEL_ID, ",".join(VOICES))
    try:
        yield
    finally:
        LOGGER.info("Stopping service")
        await service.close()


app = FastAPI(
    title="Silero TTS Bridge",
    version="0.1.0",
    description="CPU-only OpenAI-compatible speech API for Silero RU v5.5.",
    lifespan=lifespan,
)


def runtime(request: Request) -> tuple[Settings, SynthesisService]:
    return request.app.state.settings, request.app.state.service


async def authorize(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    settings: Settings = request.app.state.settings
    if not settings.api_key:
        return
    expected = f"Bearer {settings.api_key}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")


@app.get("/healthz")
async def health(request: Request) -> dict[str, object]:
    settings, service = runtime(request)
    return {
        "status": "ok" if service.engine.ready else "starting",
        "model": MODEL_ID,
        "voices": VOICES,
        "queue": {
            "active": service.active,
            "pending": service.queue.qsize(),
            "capacity": settings.max_pending,
        },
    }


@app.post("/v1/audio/speech", dependencies=[Depends(authorize)])
async def create_speech(payload: SpeechRequest, request: Request) -> Response:
    settings, service = runtime(request)
    if payload.model not in MODEL_ALIASES:
        raise HTTPException(status_code=400, detail=f"model must be {MODEL_ID}")
    if payload.voice not in VOICES:
        raise HTTPException(status_code=400, detail=f"voice must be one of: {', '.join(VOICES)}")
    if len(payload.input) > settings.max_input_chars:
        raise HTTPException(status_code=413, detail=f"input exceeds {settings.max_input_chars} characters")
    if payload.response_format.lower() not in {"opus", "ogg", "mp3", "wav"}:
        raise HTTPException(status_code=400, detail="response_format must be opus, ogg, mp3, or wav")
    started = time.monotonic()
    try:
        audio = await service.synthesize(
            payload.input,
            payload.voice,
            payload.response_format,
            payload.speed,
        )
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
    except SynthesisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    LOGGER.info(
        "Speech created: voice=%s format=%s input_chars=%s audio_bytes=%s audio_seconds=%.2f elapsed=%.2f",
        payload.voice,
        payload.response_format,
        len(payload.input),
        len(audio.data),
        audio.duration_seconds,
        time.monotonic() - started,
    )
    return Response(
        content=audio.data,
        media_type=audio.media_type,
        headers={
            "Content-Disposition": f'inline; filename="speech.{audio.extension}"',
            "X-Audio-Duration": f"{audio.duration_seconds:.3f}",
            "X-TTS-Model": MODEL_ID,
        },
    )


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, error: Exception) -> JSONResponse:
    LOGGER.exception("Unhandled request error", exc_info=error)
    return JSONResponse(status_code=500, content={"detail": "internal synthesis error"})
