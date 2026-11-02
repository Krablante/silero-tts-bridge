from __future__ import annotations

import json
import hashlib
import os
import sys
import urllib.request


def main() -> int:
    base_url = os.getenv("TTS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    api_key = os.getenv("TTS_API_KEY", "").strip()
    with urllib.request.urlopen(f"{base_url}/healthz", timeout=10) as response:
        health = json.load(response)
    if health.get("status") != "ok":
        raise RuntimeError(f"service is not ready: {health}")
    voices = ("xenia", "eugene", "aidar", "baya", "kseniya")
    if health.get("model") != "silero-ru-v5.6" or set(health.get("voices", [])) != set(voices):
        raise RuntimeError("service does not expose the expected model and voices")
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})}
    cases = [("xenia", "auto", "В 2026 году вышло 45 серий. Python, C++ и API работают.")]
    cases += [(voice, "prepared", "Это **важно**!\n\nТы *завтра* придёшь?") for voice in voices]
    total_bytes = 0
    for voice, mode, text in cases:
        identifier = None
        try:
            payload = {"model": health["model"], "input": text, "voice": voice, "text_mode": mode, "response_format": "opus"}
            request = urllib.request.Request(f"{base_url}/v1/audio/speech", data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=180) as response:
                identifier = response.headers.get("X-TTS-Job-ID")
                audio = response.read()
                content_type = response.headers.get_content_type()
                checksum = response.headers.get("X-Content-SHA256")
                model = response.headers.get("X-TTS-Model")
            if content_type != "audio/ogg" or not audio.startswith(b"OggS") or b"OpusHead" not in audio:
                raise RuntimeError("response is not valid OGG/Opus audio")
            if not identifier or model != health["model"] or checksum != hashlib.sha256(audio).hexdigest():
                raise RuntimeError("response is missing its model/job identity or failed the SHA-256 check")
            total_bytes += len(audio)
        finally:
            if identifier:
                request = urllib.request.Request(f"{base_url}/v1/audio/jobs/{identifier}?purge=true", headers=headers, method="DELETE")
                with urllib.request.urlopen(request, timeout=10):
                    pass
    print(f"ok model={health['model']} voices={len(voices)} cases={len(cases)} bytes={total_bytes}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"smoke failed: {error}", file=sys.stderr)
        raise
