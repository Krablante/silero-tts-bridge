from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import urllib.request
from pathlib import Path


DEFAULT_URL = "https://models.silero.ai/models/tts/ru/v5_5_ru.pt"
DEFAULT_SHA256 = "50081637b602126ee06cb3bc8a744d25651d2da149ee8864b9a379bfdd934437"


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def main() -> int:
    target = Path(os.getenv("SILERO_MODEL_PATH", "/models/v5_5_ru.pt"))
    expected = os.getenv("SILERO_MODEL_SHA256", DEFAULT_SHA256).strip().lower()
    url = os.getenv("SILERO_MODEL_URL", DEFAULT_URL).strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and digest(target) == expected:
        print(f"Silero model ready: {target}")
        return 0
    if target.exists():
        print(f"Removing model with invalid checksum: {target}", file=sys.stderr)
        target.unlink()
    handle, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        print(f"Downloading pinned Silero model to {target}")
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        actual = digest(temporary)
        if actual != expected:
            raise RuntimeError(f"Silero checksum mismatch: expected {expected}, got {actual}")
        temporary.replace(target)
        print(f"Silero model verified: sha256={actual}")
        return 0
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
