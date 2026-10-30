# Installation and operation

[Project](../../README.md) · [Documentation](../README.md) · [Русский](../ru/operations.md)

## Install the server

The published image targets Linux AMD64 and includes CPU PyTorch and FFmpeg. Docker Compose loads `.env` by default. Start with the [README commands](../../README.md); keep configuration private and pin `TTS_IMAGE` to a tested release or digest in production.

[`.env.example`](../../.env.example) is the configuration reference. Compose uses `TTS_BIND_ADDRESS`, `TTS_PORT`, `TTS_IMAGE`, `TTS_MODELS_PATH`, `TTS_DATA_PATH` and `TTS_ENV_FILE`; the application reads the synthesis, queue and retention settings from its environment. If using a custom env file, pass it to Compose too:

```sh
docker compose --env-file /absolute/path/to/bridge.env up -d --wait --wait-timeout 300
```

That file can set `TTS_ENV_FILE=/absolute/path/to/bridge.env` to use itself as the container environment. Compose interpolation and the container's `env_file` are separate mechanisms. Host bind directories must be writable by UID 1000; default named volumes need no manual ownership setup.

| Path | Contents | Keep on update? |
|---|---|---|
| `/models` | Downloaded, checksum-pinned model | Yes; reproducible from the official host |
| `/state` | SQLite/WAL, sources, prepared text, fragments, results, optional key file | **Yes**; needed for job recovery |
| `/tmp` | Bounded disposable tmpfs | No |

The container runs as UID/GID 1000 with a read-only root filesystem and dropped capabilities. Never remove the state volume as part of an ordinary update.

## Configure access

The default bind is localhost. For remote clients, use a private interface and HTTPS proxy or an encrypted private network. Configure `TTS_API_KEY` for one owner, or `TTS_KEYS_FILE=/state/client-keys.json` for separate applications:

```json
{"bot-a":"replace-with-a-long-random-key","bot-b":["new-random-key-at-least-24-characters","old-key-during-rotation-only"]}
```

Per-client keys must be unique and at least 24 characters. Each owner needs at least one key; empty lists stop startup. Keys under the same name share access to jobs, so overlapping keys allow rotation without moving ownership. Replace the private file and restart the service to load it; validate the new key before removing the old one. Keep files owner-only and keys out of Git, arguments and logs.

## Size and retain work

Defaults: 12,000 input characters/20 MiB output for synchronous calls; 5 million characters/2 GiB output for jobs; eight active jobs and 10 million queued characters. Each owner can occupy at most half the queue, with a minimum owner limit of two, subject to the global limit. New and resumed work use the same admission checks.

The disposable cache target is 2 GiB and the working-state budget is 20 GiB. Active model/encoder files are protected, including work still stopping after cancellation; active work can exceed the disposable cache target but remains subject to the working budget and available disk. Text, database copies and in-flight output are included in admission estimates. This is a conservative working budget, not a filesystem quota; database/index overhead and crash debris still require free disk space.

Finished results older than five minutes may be evicted under cache pressure. The default seven-day retention also expires source/prepared text and failed/cancelled work. Expired jobs report `expired`; old keys do not silently trigger synthesis. Expiration markers are retained for twice the configured retention after expiration, then their keys can be reused. Download permanent publications into the application's own storage.

Use compressed output for books. WAV's predictable PCM size is checked during synthesis; other formats are limited during assembly and checked against the complete duration. A failed output never becomes a partial successful result; compatible fragments remain reusable with another format.

## Update and recover

```sh
git pull --ff-only
docker compose pull
docker compose up -d --wait --wait-timeout 300
python3 scripts/smoke.py
```

Use the same `--env-file` on every Compose command when configured. Change the pinned `TTS_IMAGE` deliberately before pulling a release. For the smoke check, set `TTS_BASE_URL` to the server root (without `/v1`) and `TTS_API_KEY` when authenticated. The check synthesizes a short synthetic passage and validates its Opus signature, job ID and SHA-256.

Run **one Uvicorn worker**. An exclusive state lock rejects a second process. Two effective PyTorch threads are the default; they are applied after model loading and reported by `/healthz`. Measure on your hardware before increasing them.

On restart, durable work continues from committed blocks/fragments and interrupted encoding starts again from the lossless parts. Non-idempotent synchronous work is cancelled. Missing or corrupt fragments are regenerated; changed synthesis identity fails explicitly. `resume` repeats the failed stage under current queue capacity. It does not override incompatible configuration.

Back up the whole private state directory or volume with the service stopped, including key files and SQLite sidecars. Keep model storage separately or redownload the pinned artifact. Restore the complete state into a writable volume, use matching synthesis settings and start one worker. Local checkpoints do not protect against loss of the host disk.

## Diagnose

| Symptom | Check or action |
|---|---|
| Startup stays unhealthy | `docker compose logs --tail 60`; check model download/checksum, writable mounts and key-file structure |
| Model reports one thread | Use `/healthz`; ensure the running image matches the release and `SILERO_CPU_THREADS` |
| 429 on submit/resume | Wait and retry with the same key; inspect active jobs before raising capacity |
| `unsupported_text` | Inspect the affected prepared block; add a pronunciation or submit a prepared spoken copy |
| `configuration_changed` | Restore the recorded model/normalization/render settings or submit a new request/key |
| 507 / `storage_limit` | Check disk and storage budget; purge disposable terminal jobs, then resume |
| Expired/missing result | Re-submit source with a new key; CLI `--refresh` does this explicitly |
| CLI wait timed out | The job remains saved; run `status` or `wait` with its ID |

`/healthz` reports the worker's readiness and aggregate queue metadata. Logs contain operational identifiers/counts, not submitted text or provider bodies. No external broker, ASR provider or client-side inference runtime is required.
