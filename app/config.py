from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


MODEL_ID = "silero-ru-v5.6"
VOICES = ("xenia", "eugene", "aidar", "baya", "kseniya")


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    model_path: Path
    model_sha256: str
    max_input_chars: int
    max_pending: int
    cpu_threads: int
    sample_rate: int
    max_chunk_chars: int
    silence_seconds: float
    intensity: float
    peak_ceiling: float
    opus_bitrate: str
    ffmpeg_bin: str
    ffmpeg_timeout_seconds: int
    max_output_bytes: int
    warmup: bool
    state_path: Path
    keys_file: Path | None
    max_job_chars: int
    max_job_output_bytes: int
    max_queued_chars: int
    cache_max_bytes: int
    state_max_bytes: int
    retention_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=os.getenv("TTS_API_KEY", "").strip(),
            model_path=Path(os.getenv("SILERO_MODEL_PATH", "/models/v5_6_ru.pt")),
            model_sha256=os.getenv(
                "SILERO_MODEL_SHA256",
                "04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e",
            ).strip().lower(),
            max_input_chars=_integer("TTS_MAX_INPUT_CHARS", 12_000, minimum=1),
            max_pending=_integer("TTS_MAX_PENDING", 8, minimum=1),
            cpu_threads=_integer("SILERO_CPU_THREADS", 2, minimum=1),
            sample_rate=_integer("SILERO_SAMPLE_RATE", 48_000, minimum=8_000),
            max_chunk_chars=_integer("SILERO_MAX_CHUNK_CHARS", 300, minimum=20, maximum=1000),
            silence_seconds=_number("SILERO_SILENCE_SECONDS", 0.12, minimum=0, maximum=10),
            intensity=_number("SILERO_INTENSITY", 3.0, minimum=0.1),
            peak_ceiling=_number("SILERO_PEAK_CEILING", 0.98, minimum=0.1, maximum=1.0),
            opus_bitrate=os.getenv("OPUS_BITRATE", "48k").strip() or "48k",
            ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg",
            ffmpeg_timeout_seconds=_integer("FFMPEG_TIMEOUT_SECONDS", 120, minimum=1),
            max_output_bytes=_integer("TTS_MAX_OUTPUT_BYTES", 20 * 1024 * 1024, minimum=1024),
            warmup=_boolean("TTS_WARMUP", True),
            state_path=Path(os.getenv("TTS_STATE_PATH", "/state")),
            keys_file=Path(os.environ["TTS_KEYS_FILE"]) if os.getenv("TTS_KEYS_FILE") else None,
            max_job_chars=_integer("TTS_MAX_JOB_CHARS", 5_000_000, minimum=1),
            max_job_output_bytes=_integer("TTS_MAX_JOB_OUTPUT_BYTES", 2 * 1024**3, minimum=1024),
            max_queued_chars=_integer("TTS_MAX_QUEUED_CHARS", 10_000_000, minimum=1),
            cache_max_bytes=_integer("TTS_CACHE_MAX_BYTES", 2 * 1024**3, minimum=1024),
            state_max_bytes=_integer("TTS_STATE_MAX_BYTES", 20 * 1024**3, minimum=1024),
            retention_seconds=_integer("TTS_RETENTION_SECONDS", 7 * 86400, minimum=60),
        )


def _integer(name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def _number(name: str, default: float, *, minimum: float, maximum: float | None = None) -> float:
    import math
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
