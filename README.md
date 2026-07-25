# Silero TTS Bridge

A small CPU-only OpenAI-compatible speech API for the Russian Silero v5.5 model. It accepts `POST /v1/audio/speech` and returns ready-to-send audio without any Telegram, LLM, or application-specific logic.

The service is designed for easy home-server deployment:

- one container and one persistent model volume;
- pinned model URL and SHA-256 verification;
- one model loaded once into memory;
- serial inference with a bounded queue;
- OGG/Opus, MP3, and WAV output;
- standard voice, format, and speed request fields;
- optional bearer authentication;
- no GPU requirement and no generated-audio state.

## Quick Start

Requirements: Docker with the Compose plugin on a Linux AMD64 host.

```bash
git clone https://github.com/Krablante/silero-tts-bridge.git
cd silero-tts-bridge
cp .env.example .env
docker compose up -d
docker compose logs -f silero-tts-bridge
```

The first startup downloads about one hundred forty megabytes of model data into the `silero-models` volume, verifies its checksum, loads the model, and performs a short warmup. Later starts reuse the verified cache.

Check health:

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

Synthesize Telegram-ready OGG/Opus:

```bash
curl --fail-with-body \
  http://127.0.0.1:8000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  --data '{
    "model": "silero-ru-v5.5",
    "input": "Привет! Это проверка синтеза речи.",
    "voice": "xenia",
    "response_format": "opus",
    "speed": 1
  }' \
  --output speech.ogg
```

Or run the included non-destructive smoke check:

```bash
python scripts/smoke.py
```

## API

### `GET /healthz`

Reports readiness, the fixed model ID, supported voices, and current queue occupancy. Health does not require authentication.

### `POST /v1/audio/speech`

```json
{
  "model": "silero-ru-v5.5",
  "input": "Текст для озвучивания.",
  "voice": "xenia",
  "response_format": "opus",
  "speed": 1
}
```

Supported values:

- model: `silero-ru-v5.5`;
- voices: `xenia`, `eugene`, `aidar`, `baya`, `kseniya`;
- formats: `opus`/`ogg`, `mp3`, `wav`;
- speed: from `0.25` through `4.0`.

Responses are binary audio. Validation errors use FastAPI's normal JSON error format. A full queue returns HTTP 429 with `Retry-After: 5`.

Input is normalized without destroying blank-line paragraph boundaries. Every paragraph ends the current synthesis chunk,
so a caller-provided topic/server intro remains separate from the following body. Sentences inside a paragraph are still
combined up to `SILERO_MAX_CHUNK_CHARS`; oversized sentences fall back to punctuation and word boundaries.

## Authentication And Network Exposure

The default Compose configuration binds only to `127.0.0.1`. To serve a trusted LAN client, set a specific private address and a bearer token in `.env`:

```dotenv
TTS_BIND_ADDRESS=192.168.1.125
TTS_API_KEY=replace-with-a-long-random-token
```

Then send:

```text
Authorization: Bearer replace-with-a-long-random-token
```

Do not expose this model server directly to the public internet. Prefer a firewall allowlist, WireGuard, or a private reverse proxy in addition to the token.

## Configuration

All settings and their production-ready defaults are documented in `.env.example`. Common adjustments:

- `SILERO_CPU_THREADS` controls Torch CPU parallelism;
- `TTS_MAX_PENDING` bounds waiting requests;
- `TTS_MAX_INPUT_CHARS` and `TTS_MAX_OUTPUT_BYTES` bound resource use;
- `TTS_WARMUP=false` makes startup faster but leaves first-request latency;
- `SILERO_INTENSITY`, `SILERO_PEAK_CEILING`, and `SILERO_SILENCE_SECONDS` tune output while preserving the original OpenCodeBot TTS sound.

Use one Uvicorn worker. Multiple workers would load duplicate model copies and bypass the intentional serial queue.

## Model Storage

The image does not contain the Silero model. `scripts/ensure_model.py` performs an atomic download and verifies:

```text
SHA-256 50081637b602126ee06cb3bc8a744d25651d2da149ee8864b9a379bfdd934437
```

If a cached artifact fails verification, it is removed and downloaded again. Updating the model requires changing both URL and checksum deliberately.

For hosts that keep runtime state outside the source tree, invoke Compose with a state-owned env file containing
`TTS_ENV_FILE` and `TTS_MODELS_PATH` absolute paths. The public default remains a local `.env` plus the named
`silero-models` volume.

## Updating

```bash
git pull --ff-only
docker compose pull
docker compose up -d
docker compose logs --since=5m silero-tts-bridge
python scripts/smoke.py
```

Set `TTS_IMAGE` in `.env` to pin a release tag or digest. Building from source remains available through the `build` section:

```bash
docker compose build --pull
docker compose up -d
```

## Troubleshooting

- `permission denied` under `/models`: use the named Compose volume, or make a bind-mounted directory writable by container UID `1000`.
- unhealthy during first startup: follow the logs; model download and warmup are covered by a three-minute health start period.
- HTTP 401: the client bearer token differs from `TTS_API_KEY`.
- HTTP 422: inspect the JSON detail for unsupported voice, format, excessive input, or synthesis failure.
- HTTP 429: the bounded queue is full; retry later rather than increasing concurrency.

The service intentionally logs metadata and timings, never request text or audio content.

## Licenses

The bridge code is MIT licensed. The downloaded Silero model is not bundled and has its own Creative Commons Attribution-NonCommercial-ShareAlike 4.0 terms, including a non-commercial restriction. Read [MODEL-LICENSE.md](MODEL-LICENSE.md) before deployment or redistribution.
