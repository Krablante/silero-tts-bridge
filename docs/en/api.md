# HTTP API

[Project](../../README.md) · [Documentation](../README.md) · [Русский](../ru/api.md)

All `/v1` routes use optional `Authorization: Bearer <key>`. Keys select the job owner; different owners cannot inspect each other's jobs, text or audio. `/healthz` is public and exposes readiness, effective CPU threads, voices and limits without client identities or content.

## Submit speech

`POST /v1/audio/speech` waits and returns binary audio. `POST /v1/audio/jobs` returns a durable job with HTTP 202. Both accept:

```json
{
  "model":"silero-ru-v5.6",
  "input":"В 2026 году вышло 45 серий. Python и GPT-4.",
  "voice":"xenia",
  "response_format":"opus",
  "speed":1,
  "text_mode":"auto",
  "lexicon":{}
}
```

Only `input` is required. Voices are `xenia`, `eugene`, `aidar`, `baya`, `kseniya`; formats are `mp3` (default), `opus`/`ogg`, `wav`; speed is 0.25–4 with pitch preserved. `text_mode` is `auto` or `prepared`. A lexicon accepts up to 256 entries, each with a nonempty source up to 200 characters and pronunciation up to 500. Unknown fields are rejected. [Text preparation](usage.md) explains pronunciation and validation.

The default model is `silero-ru-v5.6`; any explicitly supplied ID must match it. In prepared text, `*завтра*` selects question focus and `**важно**` adds emphasis. Each marked span must be one complete Russian word, with punctuation outside the stars. `auto` does not interpret Markdown as intonation instructions.

Send a stable `Idempotency-Key` of 1–200 visible ASCII characters when retrying. A repeated key recovers the same job, including its failure/expiration; changed input or parameters returns 409. This is scoped to the owner. Keep the same key after a lost response or 429/502/503/504. Do not automatically retry ordinary non-idempotent synchronous submissions after an uncertain transport failure.

A synchronous request without a key is cancelled when its caller disconnects or the service restarts. Jobs and keyed synchronous requests survive disconnects. They do not arrange delivery into another application. The synchronous wait is limited to 15 minutes; use the jobs API for longer work.

## Manage a job

| Route | Result |
|---|---|
| `GET /v1/audio/jobs?offset=0&limit=10` | Owner's jobs; limit 1–20, bounded JSON, `next_offset` |
| `GET /v1/audio/jobs/{id}` | State, progress, normalization report, safe error, artifact links |
| `GET /v1/audio/jobs/{id}/result` | Completed audio; supports HTTP Range |
| `GET /v1/audio/jobs/{id}/source` | Explicit original UTF-8 text download |
| `GET /v1/audio/jobs/{id}/normalized` | Complete prepared text after assembly has prepared that artifact |
| `GET /v1/audio/jobs/{id}/segments?offset=0&limit=10` | Up to 30 prepared parts and their readiness |
| `DELETE /v1/audio/jobs/{id}` | Cancel between fragments; stop active encoding |
| `POST /v1/audio/jobs/{id}/resume` | Re-admit failed/cancelled work, preserving successful fragments |
| `DELETE /v1/audio/jobs/{id}?purge=true` | Permanently remove terminal artifacts; active work must stop first |
| `POST /v1/audio/normalize?offset=0&limit=8000` | Preview preparation with the same request body, without synthesis |

`normalize` accepts the synchronous input limit. Its offsets count **characters**. Text artifact Range headers and the CLI's text offsets count **bytes**. A preparing/failed job can already have saved segments even when its complete normalized artifact is unavailable.

States are `preparing`, `queued`, `running`, `assembling`, `completed`, `failed`, `cancelled`, `expired`. Progress reports source/prepared bytes, segment counts and reused cache parts. Errors include a safe code, affected segment or source-block offset when known. A job's inspection includes original request parameters, effective processing identity and result size, duration, content type, extension and SHA-256 when completed. It never implicitly returns the book text.

Resume obeys queue capacity and queued-character limits. Cancellation takes effect between model fragments; inspection can show `cancelled` while the current fragment/encoder is stopping. Resume/purge returns 409 until it stops. A changed model, normalizer or render configuration blocks unfinished incompatible work with `configuration_changed`: restore that configuration or create a new job/key.

## Verify a download

Successful audio responses contain `X-TTS-Job-ID`, `X-Audio-Duration`, `X-TTS-Model`, `X-Content-SHA256` and a checksum ETag. Compare downloaded bytes against result metadata. The CLI resumes partial downloads and verifies both size and SHA-256 before replacing the destination. Publish only a complete verified result.

| HTTP status | Meaning |
|---|---|
| 400 / 422 | Invalid request; binary synthesis failures use 422 with job details |
| 401 | Invalid or missing key when authentication is configured |
| 404 | Unknown ID or another owner's job |
| 409 | Changed idempotent intent, unfinished result, or work still stopping |
| 410 | Expired job/artifact or missing saved result |
| 413 | Input/body exceeds its limit |
| 429 | Queue/owner/queued-character capacity; `Retry-After: 5` |
| 504 | Synchronous wait timed out; keyed work remains inspectable |
| 507 | Working storage budget or disk space exhausted |

For the jobs API, errors after admission appear as `state=failed` through the status route; they are not failed submission responses. Limits and retention are described in [operation](operations.md).
