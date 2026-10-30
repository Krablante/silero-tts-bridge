# Silero TTS Bridge

[English](README.md) · [Русский](README.ru.md)

A self-hosted Russian speech API for short replies and complete books. One CPU Silero v5.5 model serves all clients: the bridge prepares text, saves progress, alternates speech fragments between jobs and produces a verified MP3, OGG/Opus or WAV file.

The OpenAI-compatible `POST /v1/audio/speech` still returns binary audio. Long requests use the same queue through a durable jobs API. Clients need neither PyTorch nor FFmpeg; the included CLI uses only Python's standard library.

## Start

Requirements: Docker Compose on Linux AMD64. The model is downloaded once, verified by SHA-256 and kept outside the image.

```sh
git clone https://github.com/Krablante/silero-tts-bridge.git
cd silero-tts-bridge
cp .env.example .env
docker compose up -d
curl --fail http://127.0.0.1:8000/healthz
```

The default bind is localhost. Set a private bind address and authentication before using another host. The Compose file has separate persistent volumes for `/models` and `/state`; **retain `/state` during updates**, since it holds jobs, original text, prepared text, cached fragments and results. Use `TTS_MODELS_PATH` and `TTS_DATA_PATH` for host-owned directories, writable by container UID 1000.

```sh
curl --fail-with-body http://127.0.0.1:8000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: first-example' \
  --data '{"model":"silero-ru-v5.5","input":"В 2026 году вышло 45 серий. Python и GPT-4.","voice":"xenia","response_format":"opus","speed":1}' \
  --output speech.ogg
```

Voices: `xenia`, `eugene`, `aidar`, `baya`, `kseniya`. Formats: `mp3`, `opus`/`ogg`, `wav`. Speed: 0.25–4, with pitch preserved.

## Use from another host

Download `silero-tts.py` from the [release](https://github.com/Krablante/silero-tts-bridge/releases) or use `client.py` from this checkout. Python 3.10+ is sufficient. Configure `SILERO_TTS_URL` and `SILERO_TTS_API_KEY`, or create a private `~/.config/silero-tts/client.json`:

```json
{"base_url":"https://your-private-speech-host.example","api_key":"your-private-client-key"}
```

Keep the directory mode 0700 and the file mode 0600. `SILERO_TTS_CONFIG` or `--config` selects another file. The URL may include `/v1`.

```sh
python3 client.py voices
python3 client.py speak --text 'Стоимость — 543.21 RUB.' --voice kseniya -o reply.ogg --format opus
python3 client.py speak book.txt --voice xenia -o book.mp3
python3 client.py speak book.txt --voice xenia --no-wait
python3 client.py status tts_REPLACE_WITH_RETURNED_ID
python3 client.py wait tts_REPLACE_WITH_RETURNED_ID -o book.mp3
python3 client.py segments tts_REPLACE_WITH_RETURNED_ID --offset 0 --limit 10
python3 client.py text tts_REPLACE_WITH_RETURNED_ID --max-chars 4000
python3 client.py cancel tts_REPLACE_WITH_RETURNED_ID
python3 client.py resume tts_REPLACE_WITH_RETURNED_ID
python3 client.py clean tts_REPLACE_WITH_RETURNED_ID
```

`speak` waits by default and prints the job ID before waiting. Interrupting the CLI preserves the server job. Repeat the same command to reuse it, or use `status`/`wait`. Downloads continue from their partial file and must match the server's size and SHA-256 before the final path is replaced. Existing different output needs `--force`. `clean` permanently removes a terminal job's text/result; cancel and wait for its current fragment first. Cached fragments shared with other jobs may remain until cleanup.

CLI JSON is capped at 32,000 bytes. `text` returns at most 8,000 characters, with a **byte** continuation offset; `segments` returns at most 30 parts. A long transcript or audio is never printed implicitly. The CLI takes text; document extraction, Telegram retrieval and publishing remain the application's responsibility.

## Text preparation and quality

The default `text_mode=auto` uses pinned `rutextnorm` and shared rules for dates, years, quantities, money, precision, abbreviations and technical names. Raw digits and Latin letters never reach the acoustic model unchecked. Blank-line paragraphs remain hard chunk boundaries. Very long inputs are prepared in bounded blocks, so a book does not monopolize the request loop.

The bridge preserves addresses and lexical content. An application may deliberately omit links or other material **before** submission; the bridge does not silently summarize or discard it. Formatting punctuation can be simplified for pronunciation.

For special names, add a `lexicon` JSON object to a request, or use CLI `--lexicon pronunciations.json`:

```json
{"Smith":"Смит","🦊":"лиса"}
```

`text_mode=prepared` accepts an already spoken Russian copy while retaining safety checks and native stress markers such as `к+ошка`. Unsupported remaining characters fail explicitly with code points and positions in the prepared block. Original text, prepared text and normalization warnings are available from the job. Warning coordinates are relative to a source block identified by its byte offset. Warnings indicate ambiguity; they do not prove an incorrect pronunciation.

Every word must survive text partitioning. Model preparation is checked for removed letters, audio parts must contain finite non-silent samples, and final duration must match every fragment plus pauses at the requested speed. An incomplete job never becomes completed. These checks do **not** prove acoustic recognition of every word or correct interpretation of every ambiguous name. No ASR provider or LLM is called by the bridge.

## HTTP API

All `/v1` routes use the same optional bearer authentication. `/healthz` is public and reports readiness, effective CPU threads, voices and queue limits without text or client identities.

| Route | Result |
|---|---|
| `POST /v1/audio/speech` | Wait for one short request; binary audio on success |
| `POST /v1/audio/jobs` | Accept full text and return a durable job with HTTP 202 |
| `GET /v1/audio/jobs?offset=0&limit=10` | This client's jobs, with bounded pagination |
| `GET /v1/audio/jobs/{id}` | State, progress, errors and artifact links |
| `GET /v1/audio/jobs/{id}/result` | Completed binary file; supports HTTP Range |
| `GET /v1/audio/jobs/{id}/source` or `/normalized` | Explicit text artifact download |
| `GET /v1/audio/jobs/{id}/segments?offset=0&limit=10` | Bounded prepared parts and readiness |
| `DELETE /v1/audio/jobs/{id}` | Cancel between fragments; interrupt active encoding |
| `POST /v1/audio/jobs/{id}/resume` | Continue a failed/cancelled job, reusing valid parts |
| `DELETE /v1/audio/jobs/{id}?purge=true` | Permanently remove terminal artifacts |
| `POST /v1/audio/normalize?offset=0&limit=8000` | Preview bounded prepared text without synthesis |

Both speech submission routes accept `model`, `input`, `voice`, `response_format`, `speed`, optional `text_mode` and `lexicon`. Pass a stable `Idempotency-Key` when retrying. Reusing it with different input/parameters returns 409. Jobs are scoped to the authenticated client. A synchronous request without that key is cancelled on disconnect/restart; durable requests survive lost connections. Neither mechanism delivers messages to another application automatically.

States are `preparing`, `queued`, `running`, `assembling`, `completed`, `failed`, `cancelled`, `expired`. Model inference runs one fragment at a time. Streaming assembly has one lane for audio up to five minutes and one for longer results, so final book encoding does not block a short reply. At most two encoders run. Restart recovers durable jobs; changed model/normalization settings explicitly block incompatible unfinished work. An explicit resume repeats the failed stage and preserves successful fragments, unless they have expired or been evicted under the documented storage budget. Request/normalization errors are explicit, and logs contain operational metadata rather than submitted text or provider bodies.

Audio responses include `X-TTS-Job-ID`, `X-Audio-Duration`, `X-TTS-Model`, `X-Content-SHA256` and an ETag. A full queue returns 429 with `Retry-After: 5`. Missing/cross-client IDs return 404; unavailable/expired artifacts return 410. Invalid requests do not echo their input.

## Authentication and storage

`TTS_API_KEY` retains the single-key setup. For separate applications, put a private JSON map in `/state/client-keys.json` and set `TTS_KEYS_FILE` to that path:

```json
{"bot-a":"replace-with-a-long-random-key","bot-b":["new-random-key-at-least-24-characters","old-key-during-rotation-only"]}
```

Keys within one client name share job ownership, allowing rotation without losing access. Different names cannot inspect one another's text/results. The internal acoustic cache can reuse identical fragments, but it has no public cross-client lookup. Do not put keys into scripts, command arguments, Git or logs. Keep the endpoint private; use HTTPS or an encrypted private network for remote connections.

Defaults are listed in `.env.example`: 12,000 characters/20 MiB for synchronous calls, 5 million characters/2 GiB output for jobs, eight active jobs, 10 million queued characters, a 2 GiB disposable cache target, a 20 GiB working-state budget and seven-day retention. Limits are checked at admission, fragment processing and assembly. Large WAV can exceed the output budget even when MP3 fits.

Active fragments are protected from cache eviction. Finished results may be evicted under cache pressure after five minutes; TTL also expires original/prepared text and failed/cancelled work. Expired jobs report `expired`, rather than silently synthesizing again under an old key. Use `--refresh` for a new CLI job. Preserve downloaded publications in the consuming application's own storage. Back up the whole private `/state` with the service stopped if you need restart recovery beyond one disk; `/models` is separately reproducible.

## Update and verify

```sh
git pull --ff-only
docker compose pull
docker compose up -d
python3 scripts/smoke.py
```

Set `TTS_BASE_URL` and `TTS_API_KEY` for the smoke check when authentication is enabled. Pin `TTS_IMAGE` to a release tag or digest in production. Run **one Uvicorn worker**; the state lock rejects a second worker. The configured CPU thread count is applied after model loading and reported by health. Two threads are the default balance; measure your hardware before increasing it.

Development checks need FFmpeg and `pip install -r requirements.txt httpx==0.28.1`, then `python -m unittest discover -s tests -v`. They use an isolated deterministic model with real lossless encoding and assembly; no model weights or external APIs are needed. Real-model smoke and recovery checks remain part of release validation.

The bridge code is MIT. The separately downloaded model has non-commercial terms; see [MODEL-LICENSE.md](MODEL-LICENSE.md).
