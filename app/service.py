from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .engine import SileroEngine, SynthesisError, _split_text
from .store import Store, JobError, digest, atomic_text, ACTIVE
from .text import normalize, TextError, VERSION

LOGGER = logging.getLogger("silero_tts_bridge.service")
INTERACTIVE_SECONDS = 300


def prepare_block(source, offset, options, maximum, signature):
    # Byte offsets survive restarts. Grammar sees sentences/paragraphs rather than tokens.
    with source.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(32_000)
        final = not stream.read(1)
    while True:
        try:
            text = data.decode("utf-8")
            break
        except UnicodeDecodeError as error:
            if error.start >= len(data) - 4:
                data = data[:error.start]
                final = False
            else:
                raise TextError("invalid_encoding", "Source is not valid UTF-8") from None
    if not final:
        candidates = [i for i in range(len(text) // 2, len(text)) if text[i].isspace() and (text[i-1] in ".!?…" or text[i:i+2] == "\n\n")]
        cut = candidates[-1] if candidates else text.rfind(" ")
        if cut < 1:
            raise TextError("unbroken_text", "An unbroken word exceeds the text preparation budget")
        text = text[:cut + 1]
    if not text.strip() and offset:
        return offset + len(text.encode()), [], {"version": VERSION, "source_characters": len(text), "normalized_characters": 0, "changed": True, "warnings": []}, final, ""
    normalized, report = normalize(text, options.get("text_mode", "auto"), options.get("lexicon"))
    for warning in report["warnings"]:
        warning["source_block_byte_offset"] = offset
    if any(len(word) > maximum for word in normalized.split()):
        raise TextError("unbroken_text", "A word exceeds the model fragment limit; supply an explicit pronunciation")
    pieces = _split_text(normalized, maximum)
    if " ".join(" ".join(pieces).split()) != " ".join(normalized.split()):
        raise TextError("partition_loss", "Text partition did not preserve every word")
    fingerprints = [(p, digest([signature, VERSION, options.get("lexicon", {}), options["voice"], p])) for p in pieces]
    return offset + len(text.encode()), fingerprints, report, final, normalized


class SynthesisService:
    def __init__(self, engine: SileroEngine, settings):
        self.engine = engine
        self.settings = settings
        self.store = Store(settings)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="silero")
        self.encoder = ThreadPoolExecutor(max_workers=2, thread_name_prefix="audio-encoder")
        self.wake = asyncio.Event()
        self.worker = None
        self.assemblies = {}
        self.stopping = False
        self.active = False
        self.current_job = None

    async def start(self, *, warmup):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self.engine.load)
        if warmup:
            await loop.run_in_executor(self.executor, self.engine.warmup)
        self.worker = asyncio.create_task(self._run(), name="speech-worker")

    async def close(self):
        self.stopping = True
        self.wake.set()
        for _, cancelled, _ in self.assemblies.values():
            cancelled.set()
        if self.worker:
            await self.worker
        if self.assemblies:
            await asyncio.gather(*(task for task, _, _ in self.assemblies.values()))
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.encoder.shutdown(wait=True, cancel_futures=True)
        self.store.close()

    def submit(self, owner, request, text, max_bytes, idempotency=None, durable=True):
        request = {**request, "_engine": self.engine.signature, "_normalizer": VERSION, "_render": [self.settings.silence_seconds, self.settings.opus_bitrate, "stream-1"]}
        job = self.store.submit(owner, request, text, max_bytes, idempotency, durable)
        self.wake.set()
        return job

    def cancel(self, identifier, owner):
        job = self.store.row(identifier, owner)
        if job["state"] in ACTIVE:
            self.store.update(identifier, state="cancelled", reserved_bytes=0)
            if identifier in self.assemblies:
                self.assemblies[identifier][1].set()
        self.wake.set()
        return self.store.row(identifier, owner)

    def is_busy(self, identifier):
        return self.current_job == identifier or (identifier in self.assemblies and not self.assemblies[identifier][0].done())

    def _options(self, job):
        options = json.loads(job["options"])
        if options.get("_engine", self.engine.signature) != self.engine.signature or options.get("_normalizer", VERSION) != VERSION or options.get("_render", [self.settings.silence_seconds, self.settings.opus_bitrate, "stream-1"]) != [self.settings.silence_seconds, self.settings.opus_bitrate, "stream-1"]:
            raise JobError("configuration_changed", "This saved job uses different synthesis settings; restore them to resume, or submit a new job")
        return options

    async def _run(self):
        loop = asyncio.get_running_loop()
        while not self.stopping:
            self.store.cleanup()
            for identifier, (task, _, _) in list(self.assemblies.items()):
                if task.done():
                    await task
                    del self.assemblies[identifier]
            # A long streamed encoding never occupies the short-answer lane.
            for short in (True, False):
                if any(kind == short for _, _, kind in self.assemblies.values()):
                    continue
                ready = self.store.ready(short, INTERACTIVE_SECONDS)
                if ready:
                    cancelled = threading.Event()
                    self.store.update(ready["id"], state="assembling")
                    task = asyncio.create_task(self._assemble(ready, cancelled))
                    self.assemblies[ready["id"]] = (task, cancelled, short)
            job = self.store.next()
            if not job:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except TimeoutError:
                    pass
                continue
            self.active = True
            self.current_job = job["id"]
            try:
                self.store.ensure_space()
                options = self._options(job)
                directory = self.store.directory(job["id"])
                if job["state"] == "preparing":
                    offset, chunks, report, final, text = await loop.run_in_executor(self.executor, prepare_block, directory / "source.txt", job["prepared_offset"], options, self.settings.max_chunk_chars, self.engine.signature)
                    atomic_text(directory / f"normalized-{job['prepared_offset']:012d}.txt", text)
                    self.store.prepared(job["id"], offset, chunks, report, final, len(text.encode()))
                else:
                    chunk = self.store.next_chunk(job["id"])
                    if not chunk:
                        continue
                    self.store.update(job["id"], state="running")
                    frames = self.store.cached(chunk["cache_key"])
                    cached = frames is not None
                    if frames is None:
                        path = self.store.cache / (chunk["cache_key"] + ".flac")
                        frames = await loop.run_in_executor(self.executor, self.engine.segment, chunk["text"], options["voice"], path)
                    self.store.chunk_done(job, chunk, frames, cached)
                    if options["response_format"] == "wav":
                        total_frames = self.store.db.execute("SELECT coalesce(sum(frames),0) FROM chunks WHERE job_id=?", (job["id"],)).fetchone()[0]
                        if total_frames * 2 / options["speed"] > options["max_output_bytes"]:
                            raise SynthesisError("output_limit: generated WAV would exceed the budget; submit mp3/opus to reuse these fragments")
            except Exception as error:
                self._fail(job["id"], error)
            finally:
                self.active = False
                self.current_job = None

    async def _assemble(self, job, cancelled):
        identifier = job["id"]
        try:
            options = self._options(job)
            directory = self.store.directory(identifier)
            rows = self.store.db.execute("SELECT cache_key,frames FROM chunks WHERE job_id=? ORDER BY n", (identifier,)).fetchall()
            if len(rows) != job["total"] or any(r["frames"] is None for r in rows):
                raise SynthesisError("incomplete_audio: not every fragment is ready")
            parts = []
            for row in rows:
                if self.store.cached(row["cache_key"]) is None:
                    self.store.update(identifier, state="queued")
                    return
                parts.append((self.store.cache / (row["cache_key"] + ".flac"), row["frames"]))
                await asyncio.sleep(0)
            duration = (sum(frames for _, frames in parts) / self.settings.sample_rate + (len(parts)-1) * self.settings.silence_seconds) / options["speed"]
            bitrate = self.settings.opus_bitrate.lower()
            opus_bytes = float(bitrate[:-1]) * 125 if bitrate.endswith("k") else float(bitrate) / 8
            byte_rate = self.settings.sample_rate * 2 if options["response_format"] == "wav" else 16000 if options["response_format"] == "mp3" else opus_bytes
            reservation = min(options["max_output_bytes"], int(duration * byte_rate * 1.1) + 4096)
            self.store.ensure_space(reservation)
            self.store.update(identifier, reserved_bytes=reservation)
            # Normalized text is an explicit downloadable artifact, never an implicit model response.
            with (directory / "normalized.txt.tmp").open("w", encoding="utf-8") as out:
                for path in sorted(directory.glob("normalized-*.txt")):
                    with path.open(encoding="utf-8") as source:
                        out.write(source.read())
                        out.write("\n\n")
            (directory / "normalized.txt.tmp").replace(directory / "normalized.txt")
            result = await asyncio.get_running_loop().run_in_executor(self.encoder, self.engine.assemble, parts, directory, options["response_format"], options["speed"], options["max_output_bytes"], cancelled)
            if self.store.row(identifier)["state"] == "assembling" and not self.stopping:
                self.store.update(identifier, state="completed", reserved_bytes=0, result=json.dumps({"bytes": result.size, "sha256": result.sha256, "duration_seconds": result.duration_seconds, "content_type": result.media_type, "extension": result.extension}))
                LOGGER.info("Speech completed: job=%s segments=%s bytes=%s duration=%.3f", identifier, len(parts), result.size, result.duration_seconds)
            else:
                result.path.unlink(missing_ok=True)
        except Exception as error:
            if not self.stopping:
                self._fail(identifier, error)
        finally:
            self.wake.set()

    def _fail(self, identifier, error):
        if self.store.row(identifier)["state"] not in ACTIVE:
            return
        code = getattr(error, "code", "synthesis_failed")
        # Model exceptions can contain input. Expose only our own safe messages.
        message = str(error) if isinstance(error, (TextError, JobError, SynthesisError)) else "Synthesis failed; successful fragments are preserved and the job can be resumed"
        job = self.store.row(identifier)
        chunk = self.store.next_chunk(identifier) if job["state"] != "preparing" else None
        self.store.fail(identifier, {"code": code, "message": message, "issues": getattr(error, "issues", []), "segment": chunk["n"] if chunk else None, "source_block_byte_offset": job["prepared_offset"] if job["state"] == "preparing" else None})
        LOGGER.error("Speech failed: job=%s code=%s error_type=%s", identifier, code, type(error).__name__)
