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
    request = urllib.request.Request(
        f"{base_url}/v1/audio/speech",
        data=json.dumps(
            {
                "model": "silero-ru-v5.5",
                "input": (
                    "Пришло новое сообщение из топика, Проверка.\n\n"
                    "В 2026 году вышло 45 серий. Python, C++ и API работают."
                ),
                "voice": "xenia",
                "response_format": "opus",
                "speed": 1,
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        audio = response.read()
        content_type = response.headers.get_content_type()
        checksum = response.headers.get("X-Content-SHA256")
        identifier = response.headers.get("X-TTS-Job-ID")
    if content_type != "audio/ogg" or not audio.startswith(b"OggS") or b"OpusHead" not in audio:
        raise RuntimeError("response is not valid OGG/Opus audio")
    if not identifier or checksum != hashlib.sha256(audio).hexdigest():
        raise RuntimeError("response is missing its job identity or failed the SHA-256 check")
    print(f"ok model={health['model']} bytes={len(audio)} content_type={content_type}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"smoke failed: {error}", file=sys.stderr)
        raise
