# Silero TTS Bridge

[English](README.md) · [Русский](README.ru.md) · [Documentation](docs/README.md) · [Releases](https://github.com/Krablante/silero-tts-bridge/releases)

**Russian speech for short replies and whole books, served by one CPU model.** The bridge prepares text, saves progress and shares synthesis time between applications. A book keeps moving while a short reply gets its turn.

Use the OpenAI-compatible binary API for an existing speech client, or submit a durable job from the included CLI. Both use the same Silero v5.5 model, pronunciation rules and lossless fragment cache. Clients need Python 3.10+; they need neither PyTorch nor FFmpeg.

```sh
python3 client.py speak book.txt --voice kseniya --format opus -o book.ogg
```

The command prints a job ID, waits and verifies the downloaded file. Interrupted waiting leaves the server job intact. Repeat the command to find it again, or use `status`, `wait`, `cancel` and `resume`.

## Start locally

You need Docker Compose on Linux AMD64. The first start downloads and verifies the model; later starts reuse it.

```sh
git clone https://github.com/Krablante/silero-tts-bridge.git
cd silero-tts-bridge
cp .env.example .env
docker compose up -d --wait --wait-timeout 300
python3 scripts/smoke.py
python3 client.py speak \
  --text 'В 2026 году Python и C++ получили 45 обновлений.' \
  --format opus -o reply.ogg
```

The default endpoint is `http://127.0.0.1:8000`. Configure authentication and a private HTTPS route before connecting other hosts. Keep the `/state` volume when updating: it contains submitted text, saved jobs and audio. See [installation and operation](docs/en/operations.md).

## Fit it into an application

```sh
curl --fail-with-body http://127.0.0.1:8000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: first-example' \
  --data '{"input":"Привет. Проверка 42.",
           "voice":"xenia","response_format":"opus"}' \
  --output speech.ogg
```

Choose `xenia`, `eugene`, `aidar`, `baya` or `kseniya`; produce MP3, OGG/Opus or WAV at 0.25–4× speed. For long text, `POST /v1/audio/jobs` returns an ID immediately. Saved progress survives service restarts, and stable request keys recover the same job after a lost response.

Numbers, dates, money and technical names are prepared centrally. Original and prepared text stay inspectable, and a per-request pronunciation dictionary handles special names. Unsupported text fails explicitly. File integrity and complete partitioning are checked; correct pronunciation of every word is not guaranteed.

## Go deeper

| Task | Guide |
|---|---|
| Install the CLI, read a book, fix a pronunciation | [Usage and text preparation](docs/en/usage.md) |
| Integrate an application | [HTTP API](docs/en/api.md) |
| Configure keys, storage, updates and recovery | [Operation](docs/en/operations.md) |
| Understand the code, run checks, publish a version | [Architecture and development](docs/en/development.md) |

The bridge code is MIT. The separately downloaded Silero model has **non-commercial** terms: [model license](docs/en/model-license.md).
