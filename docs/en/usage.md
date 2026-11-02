# CLI and text preparation

[Project](../../README.md) · [Documentation](../README.md) · [Русский](../ru/usage.md)

## Connect a client

Download `silero-tts.py` and `SHA256SUMS` from the same [release](https://github.com/Krablante/silero-tts-bridge/releases). Run `sha256sum --check SHA256SUMS` beside the files, then `python3 silero-tts.py --version`. The examples below use `client.py` from the checkout; the released script accepts the same commands. Python 3.10+ is sufficient.

Set `SILERO_TTS_URL` and `SILERO_TTS_API_KEY`, or create `~/.config/silero-tts/client.json`:

```json
{"base_url":"https://your-private-speech-host.example","api_key":"your-private-client-key"}
```

Use mode 0700 for the directory and 0600 for the file. `SILERO_TTS_CONFIG` or `--config` selects another file; environment URL/key overrides file values. The URL may end in `/v1`. Use a private HTTPS route or an encrypted private network.

## Read and recover

```sh
python3 client.py voices
python3 client.py speak book.txt --voice kseniya --format opus -o book.ogg
python3 client.py speak chapter.txt --voice xenia --no-wait
```

`speak` prints the ID before waiting. Repeat the same input and parameters to reuse its job. `--refresh` explicitly creates a new job, still reusing compatible cached fragments. Text files default to UTF-8 with optional BOM; `--encoding` selects another encoding, and `-` reads stdin.

Use the actual returned ID in these commands:

```sh
python3 client.py list --limit 10
python3 client.py status tts_REPLACE_WITH_RETURNED_ID
python3 client.py wait tts_REPLACE_WITH_RETURNED_ID -o book.ogg
python3 client.py download tts_REPLACE_WITH_RETURNED_ID -o book.ogg
python3 client.py cancel tts_REPLACE_WITH_RETURNED_ID
python3 client.py resume tts_REPLACE_WITH_RETURNED_ID
python3 client.py clean tts_REPLACE_WITH_RETURNED_ID
```

`cancel` stops between fragments and interrupts encoding. `resume` admits the job under the same queue limits as a new request. It preserves successful parts and regenerates missing or damaged ones. `clean` permanently removes a terminal job's text and result; wait for active work to stop first. Shared cached fragments can remain until cleanup.

Waiting defaults to 24 hours. A timeout exits with an error and preserves the job; Ctrl-C exits 130. Failed, cancelled and expired jobs exit 1; configuration/transport errors exit 2. `--no-wait` deliberately returns an active job successfully. Downloads continue from their `.part` file; size and SHA-256 must match before the final path is replaced. An existing different output needs `--force`.

## Inspect pronunciation

```sh
python3 client.py segments tts_REPLACE_WITH_RETURNED_ID --offset 0 --limit 10
python3 client.py text tts_REPLACE_WITH_RETURNED_ID --kind source --max-chars 4000
python3 client.py text tts_REPLACE_WITH_RETURNED_ID --kind normalized --max-chars 4000
```

CLI output is limited to 32,000 JSON bytes. `text` accepts up to 8,000 characters and returns a **byte** continuation offset; `segments` returns up to 30 parts. A complete book is never printed implicitly. Download the explicit HTTP text artifacts when you need the full copy.

`text_mode=auto` uses pinned `rutextnorm` and shared rules for dates, years, quantities, money, precision, abbreviations and technical names. Blank-line paragraphs remain hard synthesis boundaries. Original text, prepared parts and bounded warnings remain inspectable. Warning coordinates refer to a source block identified by byte offset; unsupported-character positions refer to the prepared block.

Supply special pronunciations with `--lexicon pronunciations.json`, containing, for example:

```json
{"Smith":"Смит","🦊":"лиса"}
```

Use `--text-mode prepared` for an already spoken Russian copy, including native stress markers such as `к+ошка`. Digits, Latin letters and other unsupported characters must still pass validation. Unsupported content stops synthesis with a clear error; supply a pronunciation instead of deleting it silently.

Silero v5.6 uses `?` and `!` for question and exclamation intonation. In `prepared` mode, wrap one complete Russian word in `*word*` to select the center of a question, or `**word**` for emphasis. Stress markers can appear inside that word. Put punctuation outside the stars; unpaired markers and marked phrases fail with `invalid_prosody`.

```sh
python3 client.py speak --text 'Это **важно**! Ты *завтра* придёшь?' \
  --text-mode prepared --voice xenia --format opus -o expressive.ogg
```

`auto` remains the mode for ordinary text, including Markdown; it does not interpret stars as voice instructions. The model already includes the updated Russian stress and homograph processing; no separate accentor installation is needed.

The bridge preserves addresses and lexical content. The application decides whether to omit links, email or other material before submission. Document extraction, content selection and publishing belong to that application. Formatting punctuation can be simplified for speech.

Partition checks preserve every word of the prepared text; the acoustic model's preparation is checked for removed letters. Finite, non-silent samples and complete output duration are also checked. These checks do not prove the pronunciation or interpretation of every name. The bridge calls neither ASR nor an LLM.
