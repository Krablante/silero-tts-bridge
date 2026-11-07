"""Important public contracts, with real FFmpeg and an isolated deterministic model."""
import json
import logging
import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app import main
from app.engine import SileroEngine
from app.text import normalize, TextError

logging.getLogger("httpx").setLevel(logging.WARNING)


class FakeEngine(SileroEngine):
    calls = []

    def load(self):
        self._model = True

    @property
    def threads(self):
        return 2

    def warmup(self):
        pass

    def segment(self, text, voice, target):
        self.calls.append(text)
        time.sleep(.015)
        frames = 4800
        pcm = (np.sin(np.arange(frames) * .08) * 10000).astype("<i2").tobytes()
        self._flac(pcm, target)
        return frames


def wait(client, identifier, maximum=15):
    until = time.monotonic() + maximum
    while time.monotonic() < until:
        job = client.get("/v1/audio/jobs/" + identifier).json()
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(.02)
    raise AssertionError("Job failed to settle")


class TextTests(unittest.TestCase):
    def test_client_wait_timeout_is_an_error_and_versions_match(self):
        from client import Client, VERSION
        self.assertEqual(main.app.version, VERSION)
        with self.assertRaisesRegex(ValueError, "durable job is preserved"):
            Client().wait({"id": "tts_" + "0" * 32, "state": "queued"}, 0)

    def test_numeric_and_technical_content(self):
        text, report = normalize("В 2026 году Python и GPT-4 получили 45 серий. C++, HTML5, № 12, § 3. Формула ω^2 = 4.")
        self.assertNotRegex(text, r"[A-Za-z0-9ω^]")
        for token in ("пайтон", "четыре", "сорок пять", "плюс плюс", "параграф три", "омега", "в степени"):
            self.assertIn(token, text)
        self.assertEqual(report["version"], "ru-bridge-3")
        scientific = normalize("Коэффициент 1.2e-9.")[0]
        self.assertIn("в степени минус девять", scientific)
        self.assertNotRegex(scientific, r"[A-Za-z0-9]")

    def test_paragraphs_prepared_text_and_addresses(self):
        source = "Вступление.\n\nДвадцать шесть слов."
        self.assertEqual(normalize(source)[0], source)
        self.assertEqual(normalize(source, "prepared")[0], source)
        address = normalize("Связь: https://example.com/a42 и user@example.com.")[0]
        self.assertIn("ексампле", address)
        self.assertIn("собака", address)
        self.assertIn("сорок два", address)
        with self.assertRaises(TextError):
            normalize("Версия 42", "prepared")

    def test_money_precision_and_grammar(self):
        text = normalize("До 2,5 млн рублей, цена 543.21 RUB, доля 0,000000001 и код 0007.")[0]
        self.assertIn("двух целых и пяти десятых миллиона рублей", text)
        self.assertIn("пятьсот сорок три рубля двадцать одна копейка", text)
        self.assertIn("ноль запятая ноль ноль ноль ноль ноль ноль ноль ноль один", text)
        self.assertIn("ноль ноль ноль семь", text)

    def test_governed_numbers_keep_prepositions_and_real_measurement_units(self):
        cases = {
            "до 1917 в России": "до тысячи девятисот семнадцати в России",
            "после 5 в школе": "после пяти в школе",
            "до 5 а затем": "до пяти а затем",
            "до 5 В": "до пяти вольт",
            "до 5 кВ": "до пяти киловольт",
            "до 5 А": "до пяти ампер",
            "до 5 км": "до пяти километров",
            "до 5 КМ": "до пяти километров",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalize(source)[0], expected)
                self.assertEqual(normalize(expected)[0], expected)
        text = normalize("Рост до 22 900 в 2024-м и до 5 В на выходе.")[0]
        self.assertIn("до двадцати двух тысяч девятисот в", text)
        self.assertEqual(text.count("вольт"), 1)
        self.assertNotRegex(text, r"[0-9]")

    def test_unsupported_input_and_lexicon(self):
        with self.assertRaises(TextError):
            normalize("Привет 🦊")
        self.assertIn("лиса", normalize("Привет 🦊", lexicon={"🦊": "лиса"})[0])
        self.assertIn("капитал", normalize("capital")[0])

    def test_prepared_prosody_preserves_words_and_rejects_broken_markup(self):
        from app.engine import _split_text
        source = "Это **+очень** важно! Ты *завтра* придёшь? **По-настоящему**."
        self.assertEqual(normalize(source, "prepared")[0], source)
        self.assertEqual(" ".join(_split_text(source, 30)), source)
        self.assertEqual(normalize("+Это к+ошка.", "prepared")[0], "+Это к+ошка.")
        for bad in ("**два слова**", "*завтра**", "***важно***", "сло**во**", "**важно!**", "*важно", "важно*", "* *"):
            with self.subTest(bad=bad), self.assertRaises(TextError) as caught:
                normalize(bad, "prepared")
            self.assertEqual(caught.exception.code, "invalid_prosody")
        with self.assertRaises(TextError) as caught:
            normalize("**в+ажно** и плюс +.", "prepared")
        self.assertEqual(caught.exception.code, "invalid_stress")
        # Ordinary Markdown remains content, not an implicit instruction to the voice.
        self.assertEqual(normalize("Это **очень** важно!")[0], "Это очень важно!")
        self.assertNotIn("*", normalize("Ты *завтра* придёшь?")[0])

    def test_partition_does_not_split_a_word_after_a_short_prefix(self):
        from app.engine import _split_text
        text = "Я " + "а" * 79 + " конец."
        parts = _split_text(text, 80)
        self.assertEqual(" ".join(parts), text)
        self.assertTrue(all(len(part) <= 80 for part in parts))

    def test_migrated_userduck_pronunciation_corpus(self):
        # Shared rules moved from Userduck; callers no longer carry this corpus or dependency.
        cases = {
            "в 2024 году рост составил 12,5%": "в две тысячи двадцать четвёртом году рост составил двенадцать целых и пять десятых процента",
            "из 1500 рублей осталось 350": "из тысячи пятисот рублей осталось триста пятьдесят",
            "около 500 км": "около пятисот километров",
            "от 2 до 5 тысяч человек": "от двух до пяти тысяч человек",
            "с 2022 по 2024 год": "с две тысячи двадцать второго по две тысячи двадцать четвёртый год",
            "с 2022 до 2024 года": "с две тысячи двадцать второго до две тысячи двадцать четвёртого года",
            "в 2022–2024 годах": "с две тысячи двадцать второго по две тысячи двадцать четвёртый год",
            "за 2022-2024 годы": "за период с две тысячи двадцать второго по две тысячи двадцать четвёртый год",
            "в XXI веке, после XX века и перед III веком": "в двадцать первом веке, после двадцатого века и перед третьим веком",
            "в ⅩⅪ веке": "в двадцать первом веке",
            "в xxi веке": "в двадцать первом веке",
            "прошло 2 века": "прошло два века",
            "после 2 века": "после второго века",
            "в 2024 г.": "в две тысячи двадцать четвёртом году",
            "с 29.10.2026": "с двадцать девятого октября две тысячи двадцать шестого года",
            "на 29.10.2026": "на двадцать девятое октября две тысячи двадцать шестого года",
            "к 2026-10-29": "к двадцать девятому октября две тысячи двадцать шестого года",
            "29 октября 2026 года в 10:30": "двадцать девятого октября две тысячи двадцать шестого года в десять часов тридцать минут",
            "2–5 кг": "от двух до пяти килограммов",
            "в 2–3 раза": "в два - три раза",
            "1,59 ГБ и 2 млрд рублей": "одна целая и пятьдесят девять сотых гигабайта и два миллиарда рублей",
            "1,59 гб и 25 мб": "одна целая и пятьдесят девять сотых гигабайта и двадцать пять мегабайт",
            "+5 и −12,1%": "плюс пять и минус двенадцать целых и одна десятая процента",
            "от 12,5 до 15,7%": "от двенадцати целых и пяти десятых до пятнадцати целых и семи десятых процента",
            "до 2,5 млн рублей": "до двух целых и пяти десятых миллиона рублей",
            "не менее 1,01 ГБ": "не менее одной целой и одной сотой гигабайта",
            "около 31%": "около тридцати одного процента",
            "цена $12.50 и 10 €": "цена двенадцать долларов пятьдесят центов и десять евро",
            "цена USD 12.50": "цена двенадцать долларов пятьдесят центов",
            "до 12,50 RUB": "до двенадцати рублей пятидесяти копеек",
            "₽100 и −$5.25": "сто рублей и минус пять долларов двадцать пять центов",
            "$1 млрд и 2 млрд рублей": "один миллиард долларов и два миллиарда рублей",
            "0,01 и 2,001 и 1,21%": "ноль целых и одна сотая и две целых и одна тысячная и одна целая и двадцать одна сотая процента",
            "проверка 1.59 ГБ и Python 3.11": "проверка одна целая и пятьдесят девять сотых гигабайта и пайтон три точка одиннадцать",
            "1\u202f500": "тысяча пятьсот",
            "метка abc123xyz и ноль 0007": "метка абк сто двадцать три ксыз и ноль ноль ноль ноль семь",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(normalize(original)[0], expected)
                self.assertEqual(normalize(expected)[0], expected)
        self.assertTrue(normalize("12345678901234567.89 RUB")[0].endswith("пятьсот шестьдесят семь рублей восемьдесят девять копеек"))
        self.assertEqual(normalize("3,141592653589793")[0], "три запятая один четыре один пять девять два шесть пять три пять восемь девять семь девять три")
        for text in ("7" * 4500, "7" * 4500 + " RUB"):
            self.assertEqual(normalize(text)[0].split().count("семь"), 4500)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        FakeEngine.calls = []
        self.environment = patch.dict(os.environ, {"TTS_STATE_PATH": self.temp.name, "TTS_WARMUP": "false", "SILERO_MAX_CHUNK_CHARS": "80", "TTS_API_KEY": "", "TTS_KEYS_FILE": ""})
        self.environment.start()
        self.engine = patch.object(main, "SileroEngine", FakeEngine)
        self.engine.start()

    def tearDown(self):
        self.engine.stop()
        self.environment.stop()
        self.temp.cleanup()

    def submit(self, client, text, **values):
        response = client.post("/v1/audio/jobs", json={"input": text, "response_format": "opus", **values})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_durable_speech_prepares_number_followed_by_preposition(self):
        with TestClient(main.app) as client:
            job = self.submit(client, "Рост до 22 900 в 2024-м. Напряжение до 5 В.", voice="aidar")
            result = wait(client, job["id"])
            self.assertEqual(result["state"], "completed", result.get("error"))
            self.assertGreater(result["progress"]["completed_segments"], 0)
            self.assertIn("до двадцати двух тысяч девятисот в", " ".join(FakeEngine.calls))
            audio = client.get("/v1/audio/jobs/" + job["id"] + "/result")
            self.assertEqual(audio.status_code, 200)
            self.assertTrue(audio.content.startswith(b"OggS"))
            self.assertIn(b"OpusHead", audio.content[:4096])

    def test_binary_api_idempotency_and_range(self):
        with TestClient(main.app) as client:
            data = {"input": "Проверка 42 и Python.", "response_format": "opus"}
            first = client.post("/v1/audio/speech", json=data, headers={"Idempotency-Key": "same-request"})
            self.assertEqual(first.status_code, 200, first.text if first.status_code != 200 else "")
            self.assertTrue(first.content.startswith(b"OggS"))
            calls = len(FakeEngine.calls)
            again = client.post("/v1/audio/speech", json=data, headers={"Idempotency-Key": "same-request"})
            self.assertEqual(again.content, first.content)
            self.assertEqual(len(FakeEngine.calls), calls)
            conflict = client.post("/v1/audio/speech", json={**data, "input": "Другой текст."}, headers={"Idempotency-Key": "same-request"})
            self.assertEqual(conflict.status_code, 409)
            identifier = first.headers["x-tts-job-id"]
            ranged = client.get(f"/v1/audio/jobs/{identifier}/result", headers={"Range": "bytes=0-3"})
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.content, b"OggS")

    def test_prepared_prosody_reaches_model_and_remains_distinct_in_cache(self):
        with TestClient(main.app) as client:
            marked = "Это **важно**!\n\nТы *завтра* придёшь?"
            first = wait(client, self.submit(client, marked, text_mode="prepared")["id"])
            self.assertEqual(first["state"], "completed", first)
            self.assertEqual(FakeEngine.calls, marked.split("\n\n"))
            before = len(FakeEngine.calls)
            plain = wait(client, self.submit(client, "Это важно!\n\nТы завтра придёшь?", text_mode="prepared")["id"])
            self.assertEqual(plain["progress"]["cached_segments"], 0)
            self.assertEqual(len(FakeEngine.calls), before + 2)
            broken = wait(client, self.submit(client, "Это **два слова**.", text_mode="prepared")["id"])
            self.assertEqual(broken["error"]["code"], "invalid_prosody")
            self.assertEqual(len(FakeEngine.calls), before + 2)

    def test_model_update_keeps_finished_audio_and_rejects_incompatible_work(self):
        with patch.object(main, "MODEL_ID", "previous-model"), patch.dict(os.environ, {"SILERO_MODEL_SHA256": "0" * 64}), TestClient(main.app) as client:
            finished = wait(client, self.submit(client, "Готовая озвучка.", model="previous-model")["id"])
            with patch.object(FakeEngine, "segment", side_effect=RuntimeError("interrupted")):
                unfinished = wait(client, self.submit(client, "Незавершённая озвучка.", model="previous-model")["id"])
            self.assertEqual(finished["state"], "completed", finished)
            self.assertEqual(unfinished["state"], "failed", unfinished)
        before = len(FakeEngine.calls)
        with TestClient(main.app) as client:
            saved = client.get(f"/v1/audio/jobs/{finished['id']}/result")
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.headers["x-tts-model"], "previous-model")
            self.assertEqual(client.post(f"/v1/audio/jobs/{unfinished['id']}/resume").status_code, 200)
            incompatible = wait(client, unfinished["id"])
            self.assertEqual(incompatible["error"]["code"], "configuration_changed")
            self.assertEqual(len(FakeEngine.calls), before)
            fresh = wait(client, self.submit(client, "Готовая озвучка.")["id"])
            self.assertEqual(fresh["state"], "completed", fresh)
            self.assertEqual(fresh["progress"]["cached_segments"], 0)
            rejected = client.post("/v1/audio/jobs", json={"input": "Проверка.", "model": "previous-model"})
            self.assertEqual(rejected.status_code, 400)

    def test_client_ownership_and_safe_validation(self):
        keys = Path(self.temp.name) / "keys.json"
        keys.write_text(json.dumps({"a": ["a" * 32, "c" * 32], "b": "b" * 32}))
        with patch.dict(os.environ, {"TTS_KEYS_FILE": str(keys)}), TestClient(main.app) as client:
            self.assertEqual(client.post("/v1/audio/jobs", json={"input": "Привет"}).status_code, 401)
            first = client.post("/v1/audio/jobs", json={"input": "Привет"}, headers={"Authorization": "Bearer " + "a" * 32}).json()
            self.assertEqual(client.get("/v1/audio/jobs/" + first["id"], headers={"Authorization": "Bearer " + "c" * 32}).status_code, 200)
            self.assertEqual(client.get("/v1/audio/jobs/" + first["id"], headers={"Authorization": "Bearer " + "b" * 32}).status_code, 404)
            bad = client.post("/v1/audio/jobs", json={"input": "private-book-content", "speed": "bad"}, headers={"Authorization": "Bearer " + "a" * 32})
            self.assertEqual(bad.status_code, 422)
            self.assertNotIn("private-book-content", bad.text)

    def test_empty_key_rotation_list_cannot_disable_authentication(self):
        from app.config import Settings
        keys = Path(self.temp.name) / "keys.json"
        keys.write_text(json.dumps({"a": []}))
        with patch.dict(os.environ, {"TTS_KEYS_FILE": str(keys)}):
            with self.assertRaisesRegex(RuntimeError, "at least one key"):
                main.Keys(Settings.from_env())

    def test_resume_obeys_admission_and_waiters_have_independent_lifetimes(self):
        from dataclasses import replace
        from app.config import Settings
        from app.store import Store, JobError
        store = Store(replace(Settings.from_env(), max_pending=1))
        try:
            options = {"voice": "xenia", "speed": 1}
            old = store.submit("a", options, "Первый текст.", 10000)
            store.update(old["id"], state="cancelled")
            active = store.submit("a", options, "Другой текст.", 10000)
            with self.assertRaises(JobError) as caught:
                store.resume(old["id"], "a")
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(store.row(old["id"])["state"], "cancelled")
            store.update(active["id"], state="cancelled")
            first = store.submit("a", options, "Одинаковый текст.", 10000, durable=False)
            store.update(first["id"], state="cancelled")
            second = store.submit("a", options, "Одинаковый текст.", 10000, durable=False)
            store.settings = replace(store.settings, max_pending=8)
            third = store.submit("a", options, "Одинаковый текст.", 10000)
            self.assertNotEqual(second["id"], third["id"])
            store.update(second["id"], state="cancelled")
            self.assertEqual(store.row(third["id"])["state"], "preparing")
        finally:
            store.close()

    def test_cancel_resume_and_restart_preserve_successful_parts(self):
        text = " ".join(f"Раздел {i} содержит несколько отдельных слов для проверки сохранности." for i in range(30))
        with TestClient(main.app) as client:
            job = self.submit(client, text)
            identifier = job["id"]
            until = time.monotonic() + 5
            while time.monotonic() < until:
                state = client.get("/v1/audio/jobs/" + identifier).json()
                if state["progress"]["completed_segments"] >= 2:
                    break
                time.sleep(.02)
            cancelled = client.delete("/v1/audio/jobs/" + identifier).json()
            self.assertEqual(cancelled["state"], "cancelled")
            count = len(FakeEngine.calls)
            time.sleep(.2)
            self.assertLessEqual(len(FakeEngine.calls), count + 1)
            self.assertEqual(client.post(f"/v1/audio/jobs/{identifier}/resume").status_code, 200)
        with TestClient(main.app) as client:
            result = wait(client, identifier)
            self.assertEqual(result["state"], "completed", result)
            self.assertEqual(result["progress"]["completed_segments"], result["progress"]["segments"])
            self.assertEqual(len(FakeEngine.calls), len(set(FakeEngine.calls)))

    def test_short_job_interleaves_with_long_job(self):
        with TestClient(main.app) as client:
            large = self.submit(client, " ".join(f"Глава {i} рассказывает о разных важных вещах и отдельных событиях." for i in range(40)))
            short = self.submit(client, "Это короткий ответ.")
            result = wait(client, short["id"])
            self.assertEqual(result["state"], "completed", result)
            self.assertNotEqual(client.get("/v1/audio/jobs/" + large["id"]).json()["state"], "completed")
            client.delete("/v1/audio/jobs/" + large["id"])

    def test_corrupt_part_is_regenerated_and_other_parts_reused(self):
        with TestClient(main.app) as client:
            text = "Сначала первая часть текста. " * 4 + "После этого вторая часть текста. " * 4
            first = wait(client, self.submit(client, text)["id"])
            self.assertEqual(first["state"], "completed", first)
            before = len(FakeEngine.calls)
            files = list((Path(self.temp.name) / "cache").glob("*.flac"))
            # Same-size corruption must invalidate the memoized checksum too.
            data = files[0].read_bytes()
            files[0].write_bytes(b"broken" + data[6:])
            second = wait(client, self.submit(client, text, response_format="mp3")["id"])
            self.assertEqual(second["state"], "completed", second)
            self.assertEqual(len(FakeEngine.calls), before + 1)

    def test_invalid_inputs_and_body_limits_do_not_reach_model(self):
        with patch.dict(os.environ, {"TTS_MAX_JOB_CHARS": "1000"}), TestClient(main.app) as client:
            oversized = client.post("/v1/audio/jobs", json={"input": "я" * 300_000})
            self.assertEqual(oversized.status_code, 413)
            invalid = client.post("/v1/audio/jobs", json={"input": "Текст", "voice": "unknown"})
            self.assertEqual(invalid.status_code, 400)
            failed = wait(client, self.submit(client, "Остался неизвестный символ 🦊.")["id"])
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["error"]["code"], "unsupported_text")
            self.assertEqual(FakeEngine.calls, [])

    def test_large_utf8_preparation_preserves_all_source_and_words(self):
        text = "В 2026 году выйдет 45 книг.\n\n" * 1500
        with TestClient(main.app) as client:
            identifier = self.submit(client, text)["id"]
            until = time.monotonic() + 10
            while time.monotonic() < until:
                job = client.get("/v1/audio/jobs/" + identifier).json()
                if job["progress"]["prepared_bytes"] == len(text.encode()):
                    break
                self.assertNotEqual(job["state"], "failed", job)
                time.sleep(.01)
            client.delete("/v1/audio/jobs/" + identifier)
            self.assertEqual(job["normalization"]["source_characters"], len(text))
            pieces, offset = [], 0
            while True:
                page = client.get(f"/v1/audio/jobs/{identifier}/segments?offset={offset}&limit=30").json()
                pieces.extend(s["text"] for s in page["segments"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            joined = " ".join(" ".join(pieces).split())
            self.assertEqual(joined, " ".join(normalize(text)[0].split()))

    def test_retention_expires_artifacts_without_claiming_completion(self):
        from app.config import Settings
        from app.store import Store
        with TestClient(main.app) as client:
            identifier = self.submit(client, "Проверка срока хранения.")["id"]
            self.assertEqual(wait(client, identifier)["state"], "completed")
        store = Store(Settings.from_env())
        try:
            store.db.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time()-store.settings.retention_seconds-1, identifier))
            store.db.commit()
            store.cleanup(force=True)
            self.assertEqual(store.row(identifier)["state"], "expired")
            self.assertFalse((store.jobs / identifier).exists())
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            store.close()
        with TestClient(main.app) as client:
            self.assertEqual(client.get(f"/v1/audio/jobs/{identifier}/result").status_code, 410)
            self.assertEqual(client.post(f"/v1/audio/jobs/{identifier}/resume").status_code, 410)

    def test_a_second_worker_cannot_open_live_state(self):
        from app.config import Settings
        from app.store import Store
        with TestClient(main.app):
            with self.assertRaisesRegex(RuntimeError, "already has a worker"):
                Store(Settings.from_env())

    def test_render_changes_reuse_fragments_but_not_old_result(self):
        text = "Первый абзац.\n\nВторой абзац."
        payload = {"input": text, "response_format": "opus"}
        headers = {"Idempotency-Key": "saved-intent"}
        with TestClient(main.app) as client:
            first = wait(client, client.post("/v1/audio/jobs", json=payload, headers=headers).json()["id"])
            before = len(FakeEngine.calls)
        with patch.dict(os.environ, {"SILERO_SILENCE_SECONDS": "0.5"}), TestClient(main.app) as client:
            old = client.post("/v1/audio/jobs", json=payload, headers=headers).json()
            self.assertEqual(old["id"], first["id"])
            second = wait(client, self.submit(client, text)["id"])
            self.assertNotEqual(second["id"], first["id"])
            self.assertEqual(len(FakeEngine.calls), before)
            self.assertGreater(second["result"]["duration_seconds"], first["result"]["duration_seconds"] + .3)

    def test_failed_fragment_resume_preserves_successful_work(self):
        original = FakeEngine.segment
        def failing(engine, text, voice, target):
            if len(FakeEngine.calls) == 2:
                raise RuntimeError("private text must not escape a model error")
            return original(engine, text, voice, target)
        with TestClient(main.app) as client:
            with patch.object(FakeEngine, "segment", failing):
                identifier = self.submit(client, "\n\n".join(f"Раздел {i} содержит отдельную мысль." for i in range(6)))["id"]
                failed = wait(client, identifier)
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["progress"]["completed_segments"], 2)
            self.assertNotIn("private text", json.dumps(failed))
            self.assertEqual(client.post(f"/v1/audio/jobs/{identifier}/resume").status_code, 200)
            result = wait(client, identifier)
            self.assertEqual(result["state"], "completed", result)
            self.assertEqual(len(FakeEngine.calls), result["progress"]["segments"])

    def test_output_limit_retains_parts_without_returning_partial_audio(self):
        with patch.dict(os.environ, {"TTS_MAX_JOB_OUTPUT_BYTES": "7000"}), TestClient(main.app) as client:
            text = "Первая проверка.\n\nВторая проверка."
            failed = wait(client, self.submit(client, text, response_format="wav")["id"])
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(client.get(f"/v1/audio/jobs/{failed['id']}/result").status_code, 409)
            self.assertGreater(failed["progress"]["completed_segments"], 0)
            complete = wait(client, self.submit(client, text)["id"])
            self.assertEqual(complete["state"], "completed", complete)
            self.assertGreater(complete["progress"]["cached_segments"], 0)

    def test_storage_admission_fails_before_using_the_model(self):
        with patch.dict(os.environ, {"TTS_STATE_MAX_BYTES": "1024"}), TestClient(main.app) as client:
            rejected = client.post("/v1/audio/jobs", json={"input": "Слово " * 200})
            self.assertEqual(rejected.status_code, 507)
            self.assertEqual(FakeEngine.calls, [])

    def test_late_encoding_cancellation_removes_unclaimed_output(self):
        encoded, release = threading.Event(), threading.Event()
        original = FakeEngine.assemble
        def finishing(engine, *args):
            result = original(engine, *args)
            encoded.set()
            release.wait(5)
            return result
        with patch.object(FakeEngine, "assemble", finishing), TestClient(main.app) as client:
            identifier = self.submit(client, "Поздняя отмена готовой сборки.")["id"]
            self.assertTrue(encoded.wait(5))
            self.assertEqual(client.delete("/v1/audio/jobs/" + identifier).json()["state"], "cancelled")
            release.set()
            until = time.monotonic() + 2
            directory = Path(self.temp.name) / "jobs" / identifier
            while list(directory.glob("result.*")) and time.monotonic() < until:
                time.sleep(.01)
            self.assertFalse(list(directory.glob("result.*")))

    def test_cancelled_encoder_parts_stay_protected_until_it_stops(self):
        from app.engine import SynthesisError
        entered, release = threading.Event(), threading.Event()
        def blocked(engine, parts, directory, output_format, speed, maximum, cancelled):
            entered.set()
            release.wait(5)
            raise SynthesisError("cancelled")
        with patch.object(FakeEngine, "assemble", blocked), TestClient(main.app) as client:
            identifier = self.submit(client, "Отдельная проверка защиты кеша.")["id"]
            self.assertTrue(entered.wait(5))
            client.delete("/v1/audio/jobs/" + identifier)
            store = main.app.state.service.store
            async def cleanup():
                with store.db:
                    store.db.execute("UPDATE cache SET used=0")
                store.cleanup(force=True)
                return len(list(store.cache.glob("*.flac")))
            self.assertGreater(client.portal.call(cleanup), 0)
            release.set()

    def test_preassembly_cancellation_cannot_requeue_a_job(self):
        from app.store import Store
        original = Store.cached
        cancelled = threading.Event()
        def cancel_while_validating(store, fingerprint):
            result = original(store, fingerprint)
            current = main.app.state.service
            if current.assemblies and not cancelled.is_set():
                identifier = next(iter(current.assemblies))
                current.cancel(identifier, "local")
                # Reproduce a damaged part detected after explicit cancellation.
                cancelled.set()
                return None
            return result
        with patch.object(Store, "cached", cancel_while_validating), TestClient(main.app) as client:
            identifier = self.submit(client, "Отмена перед сборкой.")["id"]
            self.assertTrue(cancelled.wait(5))
            time.sleep(.05)
            self.assertEqual(client.get("/v1/audio/jobs/" + identifier).json()["state"], "cancelled")

    def test_restart_removes_abandoned_files_and_keeps_completed_artifacts(self):
        with TestClient(main.app) as client:
            identifier = self.submit(client, "Готовый результат сохранён.")["id"]
            self.assertEqual(wait(client, identifier)["state"], "completed")
        root = Path(self.temp.name)
        directory = root / "jobs" / identifier
        (directory / "result.tmp.ogg").write_bytes(b"interrupted output")
        (directory / "concat.txt").write_text("abandoned assembly")
        (root / "cache" / ("0" * 64 + ".flac")).write_bytes(b"uncommitted fragment")
        orphan = root / "jobs" / ("tts_" + "0" * 32)
        orphan.mkdir()
        (orphan / "source.txt").write_text("uncommitted source")
        with TestClient(main.app) as client:
            self.assertEqual(client.get(f"/v1/audio/jobs/{identifier}/result").status_code, 200)
            self.assertFalse((directory / "result.tmp.ogg").exists())
            self.assertFalse((directory / "concat.txt").exists())
            self.assertFalse((root / "cache" / ("0" * 64 + ".flac")).exists())
            self.assertFalse(orphan.exists())

    def test_batch_encoding_does_not_block_short_answer(self):
        from app.engine import SynthesisError
        entered = threading.Event()
        original = FakeEngine.assemble
        def blocked(engine, parts, directory, output_format, speed, maximum, cancelled):
            if len(parts) > 2:
                entered.set()
                while not cancelled.wait(.01):
                    pass
                raise SynthesisError("cancelled")
            return original(engine, parts, directory, output_format, speed, maximum, cancelled)
        with patch("app.service.INTERACTIVE_SECONDS", .3), patch.object(FakeEngine, "assemble", blocked), TestClient(main.app) as client:
            long = self.submit(client, " ".join(f"Раздел {i} содержит несколько слов для отдельной проверки." for i in range(6)))
            self.assertTrue(entered.wait(5))
            short = wait(client, self.submit(client, "Короткий ответ.")["id"])
            self.assertEqual(short["state"], "completed", short)
            self.assertEqual(client.get("/v1/audio/jobs/" + long["id"]).json()["state"], "assembling")
            client.delete("/v1/audio/jobs/" + long["id"])


if __name__ == "__main__":
    unittest.main()
