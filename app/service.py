from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .engine import AudioResult, SileroEngine


LOGGER = logging.getLogger("silero_tts_bridge.service")


class QueueFullError(RuntimeError):
    pass


@dataclass(slots=True)
class Job:
    text: str
    voice: str
    response_format: str
    speed: float
    future: asyncio.Future[AudioResult]


class SynthesisService:
    def __init__(self, engine: SileroEngine, max_pending: int) -> None:
        self.engine = engine
        self.queue: asyncio.Queue[Job | None] = asyncio.Queue(maxsize=max_pending)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="silero")
        self.worker: asyncio.Task[None] | None = None
        self.active = False

    async def start(self, *, warmup: bool) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self.engine.load)
        if warmup:
            await loop.run_in_executor(self.executor, self.engine.warmup)
        self.worker = asyncio.create_task(self._run(), name="silero-synthesis-worker")

    async def close(self) -> None:
        if self.worker:
            await self.queue.put(None)
            await self.worker
        self.executor.shutdown(wait=True, cancel_futures=True)

    async def synthesize(self, text: str, voice: str, response_format: str, speed: float) -> AudioResult:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AudioResult] = loop.create_future()
        try:
            self.queue.put_nowait(Job(text, voice, response_format, speed, future))
        except asyncio.QueueFull as exc:
            raise QueueFullError("synthesis queue is full") from exc
        return await future

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            job = await self.queue.get()
            if job is None:
                self.queue.task_done()
                return
            self.active = True
            try:
                result = await loop.run_in_executor(
                    self.executor,
                    lambda: self.engine.synthesize(
                        job.text,
                        voice=job.voice,
                        response_format=job.response_format,
                        speed=job.speed,
                    ),
                )
                if not job.future.cancelled():
                    job.future.set_result(result)
            except Exception as exc:
                LOGGER.exception("Synthesis failed: voice=%s format=%s", job.voice, job.response_format)
                if not job.future.cancelled():
                    job.future.set_exception(exc)
            finally:
                self.active = False
                self.queue.task_done()
