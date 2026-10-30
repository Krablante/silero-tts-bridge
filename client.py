#!/usr/bin/env python3
"""Thin, standard-library-only Silero client. No model, Torch or FFmpeg on clients."""
from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

VERSION = "0.2.2"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials must never follow an unexpected provider redirect.
        return None


class Client:
    def __init__(self, config=None):
        config = config or os.environ.get("SILERO_TTS_CONFIG") or str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "silero-tts" / "client.json")
        settings = json.loads(Path(config).read_text()) if Path(config).is_file() else {}
        self.base = os.environ.get("SILERO_TTS_URL", settings.get("base_url", "http://127.0.0.1:8000")).rstrip("/").removesuffix("/v1")
        if not self.base.startswith(("http://", "https://")):
            raise ValueError("Configure an HTTP/HTTPS Silero base_url")
        self.key = os.environ.get("SILERO_TTS_API_KEY", settings.get("api_key", ""))
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, method="GET", payload=None, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        values = {"Content-Type": "application/json", **({"Authorization": "Bearer " + self.key} if self.key else {}), **(headers or {})}
        for attempt in range(3):
            try:
                return self.opener.open(urllib.request.Request(self.base + path, data, values, method=method), timeout=60)
            except urllib.error.HTTPError as error:
                if error.code in {429, 502, 503, 504} and attempt < 2:
                    try:
                        delay = min(10, max(1, float(error.headers.get("Retry-After", "2"))))
                    except ValueError:
                        delay = 2
                    error.close()
                    time.sleep(delay)
                    continue
                body = error.read(8000)
                try:
                    detail = json.loads(body)
                except ValueError:
                    detail = {"error": "Speech endpoint rejected the request"}
                raise ValueError(f"HTTP {error.code}: {json.dumps(detail, ensure_ascii=False)}") from None
            except (TimeoutError, OSError) as error:
                if attempt == 2:
                    raise ValueError("Speech endpoint is unavailable; repeat the same command to recover its saved job") from None
                time.sleep(attempt + 1)

    def json(self, path, method="GET", payload=None, headers=None):
        with self.request(path, method, payload, headers) as response:
            body = response.read(128_001)
            if len(body) > 128_000:
                raise ValueError("Endpoint returned excessive JSON")
            return json.loads(body)

    def wait(self, job, timeout):
        until = time.monotonic() + timeout
        while job["state"] in {"preparing", "queued", "running", "assembling"}:
            if time.monotonic() >= until:
                raise ValueError("Wait timed out; the durable job is preserved. Use status/wait with ID " + job["id"])
            time.sleep(.5)
            job = self.json("/v1/audio/jobs/" + job["id"])
        return job

    def download(self, job, output, force=False):
        if job["state"] != "completed":
            return job
        metadata = job["result"]
        target = Path(output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        def checksum(path):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while data := stream.read(1024 * 1024):
                    digest.update(data)
            return digest.hexdigest()
        if target.exists():
            if target.is_file() and target.stat().st_size == metadata["bytes"] and checksum(target) == metadata["sha256"]:
                return {"id": job["id"], "state": "completed", "path": str(target), "reused": True, **metadata}
            if not force:
                raise ValueError("Output already exists with different content; choose another path or use --force")
        partial = target.with_name(target.name + "." + job["id"] + ".part")
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset > metadata["bytes"]:
            partial.unlink()
            offset = 0
        if offset < metadata["bytes"]:
            with self.request(f"/v1/audio/jobs/{job['id']}/result", headers={"Range": f"bytes={offset}-"} if offset else {}) as response:
                mode = "ab" if offset and response.status == 206 else "wb"
                received = offset if mode == "ab" else 0
                with partial.open(mode) as destination:
                    while block := response.read(1024 * 1024):
                        received += len(block)
                        if received > metadata["bytes"]:
                            raise ValueError("Audio download exceeds its saved size")
                        destination.write(block)
                    destination.flush()
                    os.fsync(destination.fileno())
        if partial.stat().st_size != metadata["bytes"] or checksum(partial) != metadata["sha256"]:
            partial.unlink(missing_ok=True)
            raise ValueError("Audio download failed its size/SHA-256 check; repeat to download it again")
        partial.replace(target)
        return {"id": job["id"], "state": "completed", "path": str(target), **metadata}


def bounded_text(client, job_id, kind, offset, max_chars):
    with client.request(f"/v1/audio/jobs/{job_id}/{kind}", headers={"Range": f"bytes={offset}-{offset + max_chars * 4 - 1}"}) as response:
        if offset and response.status != 206:
            raise ValueError("Endpoint did not honor the requested byte offset")
        raw = response.read(max_chars * 4)
        decoder = codecs.getincrementaldecoder("utf-8")()
        text = decoder.decode(raw, final=False)[:max_chars]
        consumed = len(text.encode())
        content_range = response.headers.get("Content-Range", "")
        total = int(content_range.rsplit("/", 1)[1]) if "/" in content_range else int(response.headers.get("Content-Length", len(raw)))
        return {"id": job_id, "kind": kind, "text": text, "byte_offset": offset, "next_offset": offset + consumed if offset + consumed < total else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", help="Private client.json; SILERO_TTS_CONFIG also supported")
    sub = parser.add_subparsers(dest="command", required=True)
    speak = sub.add_parser("speak", help="Submit a complete text; repeat the command to reuse its durable job")
    speak.add_argument("file", nargs="?", help="UTF-8 file, or - for stdin")
    speak.add_argument("--text")
    speak.add_argument("--voice", default="xenia")
    speak.add_argument("--format", choices=["mp3", "opus", "ogg", "wav"], default="mp3")
    speak.add_argument("--speed", type=float, default=1)
    speak.add_argument("--text-mode", choices=["auto", "prepared"], default="auto")
    speak.add_argument("--lexicon", help="JSON object containing explicit pronunciations")
    speak.add_argument("--encoding", default="utf-8-sig")
    speak.add_argument("--no-wait", action="store_true")
    speak.add_argument("--refresh", action="store_true", help="Create a new job; compatible audio fragments can still be reused")
    speak.add_argument("--timeout", type=float, default=86400)
    speak.add_argument("--output", "-o")
    speak.add_argument("--force", action="store_true")
    for name in ("status", "cancel", "clean", "resume", "wait", "download", "segments", "text"):
        command = sub.add_parser(name)
        command.add_argument("id")
        if name in {"wait", "download"}:
            command.add_argument("--output", "-o")
            command.add_argument("--force", action="store_true")
        if name == "wait":
            command.add_argument("--timeout", type=float, default=86400)
        if name in {"segments", "text"}:
            command.add_argument("--offset", type=int, default=0)
        if name == "segments":
            command.add_argument("--limit", type=int, default=10)
        if name == "text":
            command.add_argument("--kind", choices=["source", "normalized"], default="normalized")
            command.add_argument("--max-chars", type=int, default=4000)
    sub.add_parser("voices")
    listing = sub.add_parser("list")
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    client = Client(args.config)
    if args.command == "voices":
        return client.json("/healthz")
    if args.command == "list":
        return client.json(f"/v1/audio/jobs?offset={args.offset}&limit={args.limit}")
    if args.command == "speak":
        if (args.text is None) == (args.file is None):
            raise ValueError("Choose one text file, stdin (-), or --text")
        if args.text is not None:
            text = args.text
        else:
            if args.file != "-" and Path(args.file).stat().st_size > 64 * 1024**2:
                raise ValueError("Text file exceeds 64 MiB; split the input or select a chapter")
            data = sys.stdin.buffer.read(64 * 1024**2 + 1) if args.file == "-" else Path(args.file).read_bytes()
            if len(data) > 64 * 1024**2:
                raise ValueError("Text input exceeds 64 MiB")
            text = data.decode(args.encoding, errors="strict")
        payload = {"model": "silero-ru-v5.5", "input": text, "voice": args.voice, "response_format": args.format, "speed": args.speed, "text_mode": args.text_mode}
        if args.lexicon:
            payload["lexicon"] = json.loads(Path(args.lexicon).read_text())
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        job = client.json("/v1/audio/jobs", "POST", payload, {"Idempotency-Key": "cli-" + (uuid4().hex if args.refresh else fingerprint)})
        if args.no_wait:
            return job
        print(json.dumps({"id": job["id"], "state": job["state"]}), file=sys.stderr, flush=True)
        job = client.wait(job, args.timeout)
        return client.download(job, args.output or (job["id"] + "." + args.format), args.force)
    if not args.id.startswith("tts_") or len(args.id) != 36 or not all(c in "0123456789abcdef" for c in args.id[4:]):
        raise ValueError("Expected an exact tts_ job ID")
    path = "/v1/audio/jobs/" + args.id
    if args.command == "cancel":
        return client.json(path, "DELETE")
    if args.command == "clean":
        return client.json(path + "?purge=true", "DELETE")
    if args.command == "resume":
        return client.json(path + "/resume", "POST", {})
    if args.command == "segments":
        return client.json(path + f"/segments?offset={args.offset}&limit={min(30,args.limit)}")
    if args.command == "text":
        if args.offset < 0 or not 1 <= args.max_chars <= 8000:
            raise ValueError("Use a non-negative byte offset and 1–8000 characters")
        return bounded_text(client, args.id, args.kind, args.offset, args.max_chars)
    job = client.json(path)
    if args.command == "wait":
        job = client.wait(job, args.timeout)
    if args.command in {"wait", "download"} and job["state"] == "completed":
        return client.download(job, args.output or (job["id"] + "." + job["result"]["extension"]), args.force)
    return job


if __name__ == "__main__":
    try:
        result = main()
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > 32000:
            raise ValueError("Response exceeds 32000 bytes; request a smaller page")
        print(encoded)
        sys.exit(1 if result.get("state") in {"failed", "cancelled", "expired"} else 0)
    except KeyboardInterrupt:
        print(json.dumps({"error": "Wait interrupted; the durable job is preserved. Use status/wait with its ID."}))
        sys.exit(130)
    except Exception as error:
        print(json.dumps({"error": str(error)[:2000]}, ensure_ascii=False))
        sys.exit(2)
