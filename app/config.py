from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


MODEL_ID = "silero-ru-v5.5"
MODEL_ALIASES = frozenset({MODEL_ID, "v5_5_ru"})
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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=os.getenv("TTS_API_KEY", "").strip(),
            model_path=Path(os.getenv("SILERO_MODEL_PATH", "/models/v5_5_ru.pt")),
            model_sha256=os.getenv(
                "SILERO_MODEL_SHA256",
                "50081637b602126ee06cb3bc8a744d25651d2da149ee8864b9a379bfdd934437",
            ).strip().lower(),
            max_input_chars=_integer("TTS_MAX_INPUT_CHARS", 12_000, minimum=1),
            max_pending=_integer("TTS_MAX_PENDING", 8, minimum=1),
            cpu_threads=_integer("SILERO_CPU_THREADS", 6, minimum=1),
            sample_rate=_integer("SILERO_SAMPLE_RATE", 48_000, minimum=8_000),
            max_chunk_chars=_integer("SILERO_MAX_CHUNK_CHARS", 300, minimum=20),
            silence_seconds=_number("SILERO_SILENCE_SECONDS", 0.12, minimum=0),
            intensity=_number("SILERO_INTENSITY", 3.0, minimum=0.1),
            peak_ceiling=_number("SILERO_PEAK_CEILING", 0.98, minimum=0.1, maximum=1.0),
            opus_bitrate=os.getenv("OPUS_BITRATE", "48k").strip() or "48k",
            ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg",
            ffmpeg_timeout_seconds=_integer("FFMPEG_TIMEOUT_SECONDS", 120, minimum=1),
            max_output_bytes=_integer("TTS_MAX_OUTPUT_BYTES", 20 * 1024 * 1024, minimum=1024),
            warmup=_boolean("TTS_WARMUP", True),
        )


def _integer(name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def _number(name: str, default: float, *, minimum: float, maximum: float | None = None) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
