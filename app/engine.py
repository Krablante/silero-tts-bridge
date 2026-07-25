from __future__ import annotations

import io
import logging
import math
import re
import subprocess
import warnings
import wave
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import Settings, VOICES


LOGGER = logging.getLogger("silero_tts_bridge.engine")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")
CLAUSE_BREAK_RE = re.compile(r"[,;:]\s+|\s+[—-]\s+")


@dataclass(frozen=True, slots=True)
class AudioResult:
    data: bytes
    media_type: str
    extension: str
    duration_seconds: float


class SynthesisError(RuntimeError):
    pass


class SileroEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._torch: Any | None = None
        self._model: Any | None = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if not self.settings.model_path.is_file():
            raise SynthesisError(f"Silero model is missing: {self.settings.model_path}")
        import torch

        torch.set_num_threads(self.settings.cpu_threads)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            model = torch.package.PackageImporter(str(self.settings.model_path)).load_pickle(
                "tts_models", "model"
            )
        model.to(torch.device("cpu"))
        self._torch = torch
        self._model = model
        available = set(getattr(model, "speakers", ()))
        missing = set(VOICES) - available
        if missing:
            LOGGER.warning("Configured voices absent from model: %s", ",".join(sorted(missing)))
        LOGGER.info(
            "Silero loaded: model=v5_5_ru device=cpu sample_rate=%s threads=%s",
            self.settings.sample_rate,
            self.settings.cpu_threads,
        )

    def warmup(self) -> None:
        result = self.synthesize("Сервис готов.", voice="xenia", response_format="opus", speed=1.0)
        LOGGER.info("Silero warmup complete: bytes=%s duration=%.2f", len(result.data), result.duration_seconds)

    def synthesize(self, text: str, *, voice: str, response_format: str, speed: float) -> AudioResult:
        if not self.ready:
            raise SynthesisError("Silero model is not loaded")
        cleaned = _normalize_text(text)
        if not cleaned:
            raise SynthesisError("input is empty")
        if len(cleaned) > self.settings.max_input_chars:
            raise SynthesisError(f"input is longer than {self.settings.max_input_chars} characters")
        if voice not in VOICES:
            raise SynthesisError(f"unsupported voice: {voice}")
        wav, duration = self._render_wav(cleaned, voice)
        encoded, media_type, extension = self._encode(wav, response_format, speed)
        if not encoded:
            raise SynthesisError("FFmpeg returned empty audio")
        if len(encoded) > self.settings.max_output_bytes:
            raise SynthesisError("encoded audio exceeds the configured size limit")
        return AudioResult(encoded, media_type, extension, duration / speed)

    def _render_wav(self, text: str, voice: str) -> tuple[bytes, float]:
        assert self._model is not None
        assert self._torch is not None
        chunks = _split_text(text, self.settings.max_chunk_chars)
        audio_parts: list[np.ndarray[Any, np.dtype[np.float32]]] = []
        silence_samples = int(self.settings.sample_rate * self.settings.silence_seconds)
        with self._torch.inference_mode():
            for index, chunk in enumerate(chunks):
                audio = self._model.apply_tts(
                    text=chunk,
                    speaker=voice,
                    sample_rate=self.settings.sample_rate,
                    put_accent=True,
                    put_stress_homo=True,
                    put_yo=True,
                    put_yo_homo=True,
                    stress_single_vowel=True,
                    intensity=self.settings.intensity,
                )
                samples = audio.detach().cpu().numpy().astype(np.float32)
                if not np.isfinite(samples).all():
                    raise SynthesisError("Silero returned non-finite samples")
                peak = float(np.max(np.abs(samples))) if samples.size else 0.0
                if peak > self.settings.peak_ceiling:
                    samples *= self.settings.peak_ceiling / peak
                audio_parts.append(samples)
                if index < len(chunks) - 1 and silence_samples:
                    audio_parts.append(np.zeros(silence_samples, dtype=np.float32))
        if not audio_parts:
            raise SynthesisError("Silero returned empty audio")
        samples = np.concatenate(audio_parts)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.settings.sample_rate)
            wav.writeframes(pcm.tobytes())
        return output.getvalue(), float(len(samples) / self.settings.sample_rate)

    def _encode(self, wav: bytes, response_format: str, speed: float) -> tuple[bytes, str, str]:
        normalized = response_format.lower()
        if normalized in {"opus", "ogg"}:
            codec = ["-c:a", "libopus", "-b:a", self.settings.opus_bitrate, "-f", "ogg"]
            media_type, extension = "audio/ogg", "ogg"
        elif normalized == "mp3":
            codec = ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"]
            media_type, extension = "audio/mpeg", "mp3"
        elif normalized == "wav":
            codec = ["-c:a", "pcm_s16le", "-f", "wav"]
            media_type, extension = "audio/wav", "wav"
        else:
            raise SynthesisError("response_format must be opus, ogg, mp3, or wav")
        command = [
            self.settings.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
        ]
        filters = _atempo_filters(speed)
        if filters:
            command.extend(["-filter:a", filters])
        command.extend(["-ar", str(self.settings.sample_rate), "-ac", "1", *codec, "pipe:1"])
        try:
            result = subprocess.run(
                command,
                input=wav,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.settings.ffmpeg_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SynthesisError("FFmpeg audio encoding failed") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise SynthesisError(f"FFmpeg audio encoding failed: {detail}")
        return result.stdout, media_type, extension


def _atempo_filters(speed: float) -> str:
    if math.isclose(speed, 1.0):
        return ""
    factors: list[float] = []
    remaining = speed
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(f"atempo={factor:.6g}" for factor in factors)


def _split_text(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    for paragraph in PARAGRAPH_SPLIT_RE.split(text):
        chunks.extend(_split_paragraph(paragraph, max_chars))
    return chunks


def _split_paragraph(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for sentence in SENTENCE_SPLIT_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            candidate = f"{current} {sentence}".strip()
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = sentence
            continue
        if current:
            chunks.append(current)
            current = ""
        pieces = _split_long_segment(sentence, max_chars)
        chunks.extend(pieces[:-1])
        current = pieces[-1]
    if current:
        chunks.append(current)
    return chunks


def _normalize_text(text: str) -> str:
    paragraphs = [" ".join(paragraph.split()) for paragraph in PARAGRAPH_SPLIT_RE.split(text)]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def _split_long_segment(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        clause_breaks = [
            match.end() for match in CLAUSE_BREAK_RE.finditer(window) if match.end() >= max_chars // 2
        ]
        cut = clause_breaks[-1] if clause_breaks else window.rfind(" ")
        if cut < max_chars // 2:
            cut = max_chars
        part = remaining[:cut].strip()
        if not part:
            part, cut = remaining[:max_chars], max_chars
        parts.append(part)
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts
