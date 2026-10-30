from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import subprocess
import os
import threading
import time
import tempfile
import warnings
from pathlib import Path
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
    path: Path
    media_type: str
    extension: str
    duration_seconds: float
    size: int
    sha256: str


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

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            model = torch.package.PackageImporter(str(self.settings.model_path)).load_pickle(
                "tts_models", "model"
            )
        model.to(torch.device("cpu"))
        # The model's packaged models/model.py sets this to one at import time.
        torch.set_num_threads(self.settings.cpu_threads)
        self.actual_threads = torch.get_num_threads()
        if self.settings.sample_rate not in {8000, 24000, 48000}:
            raise SynthesisError("sample_rate must be 8000, 24000 or 48000")
        self._torch = torch
        self._model = model
        available = set(getattr(model, "speakers", ()))
        missing = set(VOICES) - available
        if missing:
            LOGGER.warning("Configured voices absent from model: %s", ",".join(sorted(missing)))
        LOGGER.info(
            "Silero loaded: model=v5_5_ru device=cpu sample_rate=%s threads=%s",
            self.settings.sample_rate,
            torch.get_num_threads(),
        )

    def warmup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frames = self.segment("Сервис готов.", "xenia", Path(directory) / "warmup.flac")
        LOGGER.info("Silero warmup complete: frames=%s", frames)

    @property
    def threads(self):
        return getattr(self, "actual_threads", None)

    @property
    def signature(self):
        return [self.settings.model_sha256, self.settings.sample_rate, self.settings.intensity, self.settings.peak_ceiling, "accent-homo-yo-1"]

    def segment(self, text: str, voice: str, target: Path) -> int:
        if not self.ready:
            raise SynthesisError("Silero model is not loaded")
        if voice not in VOICES:
            raise SynthesisError(f"unsupported voice: {voice}")
        assert self._model is not None
        assert self._torch is not None
        package = self._model.packages[self._model.speaker_to_package[voice]]
        prepared = package.prepare_text_input(text, "ru")[0]
        letters = lambda s: "".join(c for c in s.lower() if c.isalpha())
        if letters(text) != letters(prepared):
            raise SynthesisError("model_text_loss: model preparation would remove letters")
        with self._torch.inference_mode():
            audio = self._model.apply_tts(text=text, speaker=voice, sample_rate=self.settings.sample_rate, put_accent=True, put_stress_homo=True, put_yo=True, put_yo_homo=True, stress_single_vowel=True, intensity=self.settings.intensity)
        samples = audio.detach().cpu().numpy().astype(np.float32)
        if not samples.size or not np.isfinite(samples).all():
            raise SynthesisError("Silero returned empty or non-finite samples")
        peak = float(np.max(np.abs(samples)))
        if peak <= 1e-7:
            raise SynthesisError("Silero returned silent audio")
        if peak > self.settings.peak_ceiling:
            samples *= self.settings.peak_ceiling / peak
        self._flac((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes(), target)
        return len(samples)

    def _flac(self, pcm: bytes, target: Path):
        temporary = target.with_suffix(".tmp.flac")
        try:
            result = subprocess.run([self.settings.ffmpeg_bin, "-y", "-nostdin", "-v", "error", "-f", "s16le", "-ar", str(self.settings.sample_rate), "-ac", "1", "-i", "pipe:0", "-c:a", "flac", "-compression_level", "1", str(temporary)], input=pcm, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=self.settings.ffmpeg_timeout_seconds)
            if result.returncode or temporary.stat().st_size < 100:
                raise SynthesisError("Lossless fragment encoding failed")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def assemble(self, parts: list[tuple[Path, int]], directory: Path, response_format: str, speed: float, max_bytes: int, cancelled: threading.Event) -> AudioResult:
        if not parts:
            raise SynthesisError("Cannot assemble an empty job")
        if response_format in {"opus", "ogg"}:
            codec = ["-c:a", "libopus", "-b:a", self.settings.opus_bitrate, "-f", "ogg"]
            media_type, extension = "audio/ogg", "ogg"
        elif response_format == "mp3":
            codec = ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"]
            media_type, extension = "audio/mpeg", "mp3"
        elif response_format == "wav":
            codec = ["-c:a", "pcm_s16le", "-f", "wav"]
            media_type, extension = "audio/wav", "wav"
        else:
            raise SynthesisError("response_format must be opus, ogg, mp3, or wav")
        pause = round(self.settings.sample_rate * self.settings.silence_seconds)
        silence = directory / "silence.flac"
        if pause:
            self._flac(bytes(pause * 2), silence)
        listing = directory / "concat.txt"
        def quoted(path):
            return "file '" + str(path.resolve()).replace("'", "'\\''") + "'\n"
        with listing.open("w") as out:
            for i, (path, frames) in enumerate(parts):
                if not path.is_file() or frames <= 0:
                    raise SynthesisError("Missing or invalid audio fragment")
                out.write(quoted(path))
                if pause and i < len(parts) - 1:
                    out.write(quoted(silence))
        expected = (sum(frames for _, frames in parts) + max(0, len(parts) - 1) * pause) / self.settings.sample_rate / speed
        if response_format == "wav" and expected * self.settings.sample_rate * 2 + 128 > max_bytes:
            raise SynthesisError("output_limit: WAV would exceed the output budget; choose opus/mp3 or increase the configured budget")
        command = [self.settings.ffmpeg_bin, "-y", "-nostdin", "-v", "error", "-xerror", "-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0:a:0"]
        filters = _atempo_filters(speed)
        if filters:
            command.extend(["-filter:a", filters])
        temporary = directory / ("result.tmp." + extension)
        command.extend(["-ar", str(self.settings.sample_rate), "-ac", "1", *codec, "-fs", str(max_bytes), str(temporary)])
        try:
            with (directory / "encode.log").open("wb") as log:
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
                started = time.monotonic()
                try:
                    while process.poll() is None:
                        if cancelled.wait(.1):
                            raise SynthesisError("cancelled")
                        if time.monotonic() - started > self.settings.ffmpeg_timeout_seconds + expected * .1:
                            raise SynthesisError("FFmpeg encoding timed out")
                    if process.returncode:
                        raise SynthesisError("FFmpeg could not decode/assemble every fragment")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            if cancelled.is_set():
                raise SynthesisError("cancelled")
            metadata = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name", "-of", "json", str(temporary)], timeout=30))
            duration = float(metadata["format"]["duration"])
            size = temporary.stat().st_size
            if size >= max_bytes or not math.isfinite(duration) or abs(duration - expected) > max(.35, expected * .005):
                raise SynthesisError("incomplete_audio: encoded duration/size does not match all fragments")
            digest = hashlib.sha256()
            with temporary.open("rb") as stream:
                while data := stream.read(1024 * 1024):
                    if cancelled.is_set():
                        raise SynthesisError("cancelled")
                    digest.update(data)
                os.fsync(stream.fileno())
            target = directory / ("result." + extension)
            temporary.replace(target)
            return AudioResult(target, media_type, extension, duration, size, digest.hexdigest())
        finally:
            temporary.unlink(missing_ok=True)
            listing.unlink(missing_ok=True)
            silence.unlink(missing_ok=True)
            (directory / "encode.log").unlink(missing_ok=True)


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
