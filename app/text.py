"""Shared deterministic speech preparation; content selection belongs to callers."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from rutextnorm import normalize_russian, number_to_ordinal_words, number_to_words, number_to_words_case, flag_uncertain

VERSION = "ru-bridge-1"
DIGITS = ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
PLACES = ("десят", "сот", "тысячн", "десятитысячн", "стотысячн", "миллионн")
GENITIVE = r"около|более|менее|свыше|без|от|до|из|после"
TERMS = {"C++": "си плюс плюс", "C#": "си шарп", "Python": "пайтон", "PostgreSQL": "постгрес кью эл", "SQLite": "эс кью лайт", "API": "эй пи ай", "CLI": "си эл ай", "GPT": "джи пи ти", "HTML": "эйч ти эм эл", "FFmpeg": "эф эф эм пег", "Groq": "грок", "Silero": "силеро", "Whisper": "виспер", "OpenAI": "оупен эй ай", "OpenCode": "оупенкод", "OpenCodez": "оупенкодз", "GitHub": "гитхаб", "Docker": "докер"}
SYMBOLS = {"^": " в степени ", "_": " подчёркивание ", "|": " вертикальная черта ", "\\": " обратный слэш ", "~": " тильда ", "`": "", "*": " звёздочка ", "≤": " меньше или равно ", "≥": " больше или равно ", "≠": " не равно ", "·": ", ", "•": ", "}
CURRENCIES = {
    "rub": (("рубль", "рубля", "рублей"), ("копейка", "копейки", "копеек"), False, True),
    "usd": (("доллар", "доллара", "долларов"), ("цент", "цента", "центов"), False, False),
    "eur": (("евро",) * 3, ("евроцент", "евроцента", "евроцентов"), False, False),
    "gbp": (("фунт", "фунта", "фунтов"), ("пенс", "пенса", "пенсов"), False, False),
    "uah": (("гривна", "гривны", "гривен"), ("копейка", "копейки", "копеек"), True, True),
}
MONEY = re.compile(rf"(?<![\w.,])(?:(?P<prep>{GENITIVE})\s+)?(?:(?P<symbol>[$€£₽₴]|RUB|USD|EUR|GBP|UAH)\s*(?P<before>\d+(?:[.,]\d{{1,2}})?)(?![\d.,]|\s*(?:тыс|млн|млрд|трлн|тысяч|миллион|миллиард|триллион))|(?P<after>\d+(?:[.,]\d{{1,2}})?)\s*(?P<currency>[$€£₽₴]|RUB|USD|EUR|GBP|UAH|руб(?:лей|ля|ль)?\.?|доллар(?:ов|а|ы)?|евро|фунт(?:ов|а|ы)?|грив(?:ен|ны|на))(?!\w))", re.I)
GEN_QUANTITY = re.compile(rf"\b({GENITIVE})\s+((?:минус|плюс)\s+)?(\d+(?:,\d+)?)(?:\s*(%|(?:тыс|млн|млрд|трлн|ГБ|МБ|КБ|ТБ|кг|км/ч|км|см|мм|м|л|ч|с|°[CСF])\.?)(?!\w))?", re.I)


class TextError(ValueError):
    def __init__(self, code, message, issues=None):
        self.code, self.issues = code, issues or []
        super().__init__(message)


def integer(digits):
    if len(digits) > 18 or (len(digits) > 1 and digits[0] == "0"):
        return " ".join(DIGITS[int(char)] for char in digits)
    return number_to_words(int(digits))


def money(match):
    token = (match["symbol"] or match["currency"]).lower().rstrip(".")
    code = {"$": "usd", "€": "eur", "£": "gbp", "₽": "rub", "₴": "uah"}.get(token, token[:3])
    code = next((key for stem, key in (("руб", "rub"), ("дол", "usd"), ("евр", "eur"), ("фун", "gbp"), ("гри", "uah")) if token.startswith(stem)), code)
    main, minor, main_feminine, minor_feminine = CURRENCIES[code]
    whole, _, fraction = (match["before"] or match["after"]).replace(",", ".").partition(".")
    cents = int(fraction.ljust(2, "0") or "0")
    def count(digits, forms, feminine):
        n = int(digits[-2:])
        one = n % 10 == 1 and n % 100 != 11
        if match["prep"]:
            words = number_to_words_case(int(digits), "gen") if len(digits) <= 18 else integer(digits)
            if feminine:
                words = re.sub(r"\bодного\b", "одной", words)
            noun = forms[1] if one else forms[2]
        else:
            words = integer(digits)
            if feminine:
                words = re.sub(r"\bодин$", "одна", words)
                words = re.sub(r"\bдва$", "две", words)
            noun = forms[0] if one else forms[1] if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else forms[2]
        return words + " " + noun
    result = count(whole.lstrip("0") or "0", main, main_feminine)
    if cents:
        result += " " + count(str(cents), minor, minor_feminine)
    return (match["prep"] + " " if match["prep"] else "") + result


def gen_quantity(match):
    prep, sign, value, unit = match.groups()
    prefix = prep + " " + (sign or "")
    if "," not in value:
        if unit != "%" or len(value) > 18:
            return match.group()
        n = int(value)
        return prefix + number_to_words_case(n, "gen") + (" процента" if n % 10 == 1 and n % 100 != 11 else " процентов")
    whole, fraction = value.split(",")
    if len(fraction) > len(PLACES) or len(whole) > 18:
        return match.group()
    nominal = normalize_russian(value)
    spoken = normalize_russian(value + (" " + unit if unit else ""))
    if not spoken.startswith(nominal):
        return match.group()
    first, last = int(whole), int(fraction)
    feminine = lambda n: re.sub(r"\bодного\b", "одной", number_to_words_case(n, "gen"))
    words = feminine(first) + (" целой" if first % 10 == 1 and first % 100 != 11 else " целых")
    words += " и " + feminine(last) + " " + PLACES[len(fraction) - 1]
    words += "ой" if last % 10 == 1 and last % 100 != 11 else "ых"
    return prefix + words + spoken[len(nominal):]


def century(match):
    token, noun = match.groups()
    noun = noun.lower()
    if token.isdecimal():
        before = match.string[max(0, match.start() - 50):match.start()]
        if noun == "века" and not re.search(r"\b(?:до|после|из|от|с|начала|конца|середины)\s+$", before, re.I):
            return match.group()
        value = int(token)
    else:
        token = token.upper()
        if not re.fullmatch(r"M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})", token):
            return match.group()
        value, previous = 0, 0
        for char in reversed(token):
            current = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}[char]
            value += -current if current < previous else current
            previous = current
    form = {"век": "nom_m", "века": "gen", "веку": "dat", "веке": "prep", "веком": "prep"}[noun]
    words = number_to_ordinal_words(value, form)
    if noun == "веком":
        words = words[:-2] + ("им" if words.endswith("ем") else "ым")
    return f"{words} {noun}"


def date_context(match):
    prep, day, month, year = match.groups()
    try:
        date(int(year), int(month), int(day))
    except ValueError:
        return match.group()
    form = "nom_n" if prep.lower() == "на" else "dat" if prep.lower() == "к" else "gen"
    return f"{prep} {number_to_ordinal_words(int(day), form)} {MONTHS[int(month) - 1]} {number_to_ordinal_words(int(year), 'gen')} года"


def numeric_range(match):
    first, last = match.groups()
    before = match.string[max(0, match.start() - 50):match.start()]
    if re.search(r"\b(?:в|на|за|через)\s+$", before, re.I):
        return normalize_russian(first) + " - " + last
    if re.search(r"\b(?:от|с)\s+$", before, re.I):
        return first + " до " + last
    if re.search(rf"\b(?:{GENITIVE})\s+$", before, re.I):
        return first + "-" + last
    return "от " + first + " до " + last


def normalize(text: str, mode="auto", lexicon=None):
    original = text
    text = unicodedata.normalize("NFC", text).replace("\u00ad", "").replace("\ufeff", "")
    text = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)
    if "\ufffd" in text or "\x00" in text:
        raise TextError("invalid_encoding", "Input contains a replacement character or NUL; provide valid text")
    lexicon = lexicon or {}
    if len(lexicon) > 256 or any(not isinstance(k, str) or not k or len(k) > 200 or not isinstance(v, str) or not v.strip() or len(v) > 500 for k, v in lexicon.items()):
        raise TextError("invalid_lexicon", "Use at most 256 non-empty pronunciation entries (source <=200, pronunciation <=500 characters)")
    warnings = []
    if mode == "auto":
        # The input-only router is advisory: it cannot prove pronunciation or grammar.
        flagged = flag_uncertain(text)
        warnings = [{"start": a, "end": b, "reason": reason} for a, b, _, reason in flagged[:16]]
        text = re.sub(r"(?<!\w)([+−-]?\d+(?:[.,]\d+)?)\s*[eE]\s*([+−-]?)(\d+)(?!\w)", lambda m: m[1] + " умножить на десять в степени " + ("минус " if m[2] in {"-", "−"} else "плюс " if m[2] == "+" else "") + integer(m[3]), text)
        terms = {**TERMS, **lexicon}
        # Product versions retain dot separators before names are transliterated.
        version_names = "|".join(re.escape(k) for k in terms)
        text = re.sub(rf"\b({version_names}|версия|версии|версию)\s+(\d+(?:\.\d+)+)", lambda m: m[1] + " " + " точка ".join(integer(p) for p in m[2].split(".")), text, flags=re.I)
        pattern = re.compile(r"(?<![A-Za-zА-Яа-яЁё])(?:" + "|".join(re.escape(k) for k in sorted(terms, key=len, reverse=True)) + r")(?![A-Za-zА-Яа-яЁё])", re.I)
        folded = {k.casefold(): v for k, v in terms.items()}
        text = pattern.sub(lambda m: folded[m[0].casefold()], text)
        text = re.sub(r"(?<=\d)(\s*)([кмгт]б)\b", lambda m: m[1] + m[2].upper(), text, flags=re.I)
        year = r"[12]\d{3}"
        noun = r"(?:год(?:а|у|ах|ы|ов)?|гг?\.)(?!\w)"
        text = re.sub(rf"\b(с|от)\s+({year})\s+(по|до)\s+({year})(?=\s+{noun})", lambda m: f"{m[1]} {number_to_ordinal_words(int(m[2]), 'gen')} {m[3]} {number_to_ordinal_words(int(m[4]), 'gen' if m[3].lower() == 'до' else 'nom_m')}", text, flags=re.I)
        text = re.sub(rf"\b(?:(в|за|на)\s+)?({year})\s*[-–—]\s*({year})\s*{noun}", lambda m: (f"{m[1]} период " if m[1] and m[1].lower() in {"за", "на"} else "") + f"с {number_to_ordinal_words(int(m[2]), 'gen')} по {number_to_ordinal_words(int(m[3]))} год", text, flags=re.I)
        text = re.sub(rf"\b(в|к|до|с|от|после|за|на)\s+({year})\s*г\.(?!\w)", lambda m: f"{m[1]} {number_to_ordinal_words(int(m[2]), {'в':'prep','к':'dat','за':'nom_m','на':'nom_m'}.get(m[1].lower(),'gen'))} " + ("год" if m[1].lower() in {"за", "на"} else "году" if m[1].lower() in {"в", "к"} else "года"), text, flags=re.I)
        text = re.sub(r"[\u2160-\u217f]+", lambda m: unicodedata.normalize("NFKC", m[0]).upper(), text)
        text = re.sub(r"\b([IVXLCDM]+|\d{1,2})\s+(век(?:а|у|е|ом)?)\b", century, text, flags=re.I)
        text = re.sub(r"(?<!\w)([+−-])\s*(?=\d|[$€£₽₴])", lambda m: "плюс " if m[1] == "+" else "минус ", text)
        text = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", r"\3.\2.\1", text)
        text = re.sub(r"\b(с|со|до|от|после|к|на)\s+(\d{1,2})\.(\d{1,2})\.(\d{4})\b", date_context, text, flags=re.I)
        text = MONEY.sub(money, text)
        text = re.sub(r"(?<![\w.,])\d+\.\d+(?![\w.]|,\d)", lambda m: m[0] if re.search(r"[A-Za-z][\w-]*\s+$", m.string[max(0,m.start()-100):m.start()]) else m[0].replace(".", ","), text)
        text = re.sub(r"(?<![\w.,-])(\d+(?:,\d+)?)\s*[–—]\s*(\d+(?:,\d+)?)(?![\w.,-])", numeric_range, text)
        text = re.sub(r"(?<![\w.,])(\d+)[,.](\d{7,})(?!\d)", lambda m: integer(m[1]) + " запятая " + " ".join(DIGITS[int(c)] for c in m[2]), text)
        text = re.sub(r"\d{19,}", lambda m: integer(m[0]), text)
        text = GEN_QUANTITY.sub(gen_quantity, text)
        text = normalize_russian(text)
        text = re.sub(r"\bодна (десятых|сотых|тысячных|десятитысячных|стотысячных|миллионных)\b", lambda m: "одна " + m[1][:-2] + "ая", text)
        text = re.sub(r"\d+", lambda m: " " + integer(m[0]) + " ", text)
        for symbol, spoken in SYMBOLS.items():
            text = text.replace(symbol, spoken)
        # '+' is a native stress marker only immediately before a Cyrillic vowel.
        text = re.sub(r"(?<![а-яё])\+|\+(?![аеёиоуыэюя])", " плюс ", text, flags=re.I)
    elif mode != "prepared":
        raise TextError("invalid_text_mode", "text_mode must be auto or prepared")
    elif re.search(r"(?<![а-яё])\+|\+(?![аеёиоуыэюя])", text, flags=re.I):
        raise TextError("invalid_stress", "Prepared text may use '+' only as a stress marker before a Cyrillic vowel")
    text = text.translate(str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "’": "'", "–": "-", "—": "-"}))
    # These punctuation marks may be discarded by the acoustic model; no lexical content is removed.
    text = re.sub(r'''[()\[\]{}"']''', " ", text)
    issues = [{"offset": m.start(), "codepoint": f"U+{ord(m[0]):04X}"} for m in re.finditer(r"[^а-яёА-ЯЁ\s.,!?:;…+\-]", text)]
    if issues:
        raise TextError("unsupported_text", "Text contains unsupported characters after preparation; supply lexicon pronunciations", issues[:16])
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n+", text)]
    text = "\n\n".join(p for p in paragraphs if p)
    if not re.search(r"[а-яё]", text, re.I):
        raise TextError("empty_speech", "Input contains no pronounceable words")
    return text, {"version": VERSION, "source_characters": len(original), "normalized_characters": len(text), "changed": text != original, "warnings": warnings}
