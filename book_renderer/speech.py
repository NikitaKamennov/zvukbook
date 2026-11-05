"""Sanitize book text; keep only validated manual stress in prepared speech."""
import re
import unicodedata
from html import unescape

from .numerals import integer_words, verbalize_numbers
from .abbreviations import expand_book_abbreviations


_STARS = str.maketrans({character: "*" for character in "＊﹡∗⁎✱"})
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INVISIBLE = str.maketrans("", "", "\u00ad\u061c\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff")
_PLAIN_PUNCTUATION = frozenset(".,!?;:-+")
_LATIN = dict(zip("abcdefghijklmnopqrstuvwxyz", ("а", "б", "к", "д", "э", "ф", "г", "х", "и", "дж", "к", "л", "м", "н", "о", "п", "к", "р", "с", "т", "у", "в", "в", "кс", "и", "з")))
_LETTER_NAMES = dict(zip("бвгджзйклмнпрстфхцчшщ", ("бэ", "вэ", "гэ", "дэ", "жэ", "зэ", "й", "ка", "эль", "эм", "эн", "пэ", "эр", "эс", "тэ", "эф", "ха", "цэ", "че", "ша", "ща")))
_CONSONANT_ABBREVIATION = re.compile(r"(?<!\w)[" + "".join(_LETTER_NAMES).upper() + r"]{2,8}(?![а-яёА-ЯЁ])")
_MIXED_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_LATIN_LOOKALIKES = str.maketrans("ABCEHKMOPTXYaceopxy", "АВСЕНКМОРТХУасеорху")
_UNSPOKEN = re.compile(r"[^а-яёА-ЯЁ\s.,!?;:—–-]")
_UNSPOKEN_SPEECH = re.compile(r"[^а-яёА-ЯЁ\s.,!?;:—–+\-]")
_UNSPOKEN_MARKED = re.compile(r"[^а-яёА-ЯЁ\s.,!?;:—–\-\ue000]")
_GREEK = dict(zip("αβγδεζηθικλμνξοπρστυφχψω", ("альфа", "бета", "гамма", "дельта", "эпсилон", "дзета", "эта", "тета",
               "йота", "каппа", "лямбда", "мю", "ню", "кси", "омикрон", "пи", "ро", "сигма", "тау", "ипсилон", "фи", "хи", "пси", "омега")))
VERSION = 4


class TextSynthesisError(ValueError):
    pass


def normalize_ocr_letters(text):
    """Repair visual Latin lookalikes only inside an otherwise Cyrillic token.

    All-Latin formulae, names and Roman numerals stay intact here. They receive
    their separate spoken representation later, rather than an OCR whitelist.
    """
    def repair(match):
        word = match.group()
        latin = [character for character in word if character.isascii()]
        if latin and len(latin) < len(word) and all(ord(character) in _LATIN_LOOKALIKES for character in latin):
            return word.translate(_LATIN_LOOKALIKES)
        return word
    return _MIXED_WORD.sub(repair, text)


def validate_speech_text(text):
    """Fail with character identities if an unprepared fragment reaches TTS."""
    invalid = list(dict.fromkeys(match.group() for match in _UNSPOKEN_SPEECH.finditer(text)))
    if invalid:
        details = "; ".join(f"{character!r} U+{ord(character):04X} {unicodedata.name(character, 'UNKNOWN')}"
                            for character in invalid[:6])
        raise TextSynthesisError("После подготовки остались неподдерживаемые символы: " + details)
    for token in re.findall(r"[а-яёА-ЯЁ+]+", text):
        if "+" in token and not re.fullmatch(r"[а-яёА-ЯЁ]*\+[аеёиоуыэюяАЕЁИОУЫЭЮЯ][а-яёА-ЯЁ]*", token):
            raise TextSynthesisError("Некорректная отметка ударения: нужен один + перед гласной внутри слова.")
    if not re.search(r"[а-яёА-ЯЁ]", text):
        raise TextSynthesisError("Во фрагменте после подготовки нет русских слов для озвучки.")


def prepare_text(text, preserve_stress=False):
    """Keep narration, remove star commands before splitting sentences/chunks.

    Asterisks in an uploaded book are footnotes, formatting or separators, not
    Silero focus/emphasis instructions. Preserve numeric multiplication/powers
    instead of turning `2*3` into `23`. The stored book is never modified here.
    """
    if preserve_stress:
        text = re.sub(r"(?<=[аеёиоуыэюяАЕЁИОУЫЭЮЯ])\u0301", "\ue000", text)
    text = unescape(text).replace("№", " номер ")
    text = "".join(str(unicodedata.decimal(character)) if character.isdecimal() else character for character in text)
    text = expand_book_abbreviations(text)
    text = re.sub(r"(?<=[0-9])([⁰¹²³⁴⁵⁶⁷⁸⁹]+)",
                  lambda match: " в степени " + unicodedata.normalize("NFKC", match.group()), text)
    text = unicodedata.normalize("NFKC", text.translate(_STARS).translate(_INVISIBLE))
    text = text.replace("\u0301", "").replace("\u0300", "")
    text = _CONTROLS.sub(" ", text)
    text = normalize_ocr_letters(text)
    # Uploaded text is never SSML, a phoneme command or an emphasis program.
    text = re.sub(r"</?[A-Za-z][^>\n]{0,200}>", " ", text)
    text = re.sub(r"(?<=\d)\s*[×·]\s*(?=\d)", " умножить на ", text)
    text = re.sub(r"(?<=\d)\s*/\s*(?=\d)", " дробь ", text)
    text = re.sub(r"(?<=\d)\s*\^\s*(?=[+-]?\d)", " в степени ", text)
    text = re.sub(r"(?<=\d)[^\S\r\n]*\*\*[^\S\r\n]*(?=\d)", " в степени ", text)
    text = re.sub(r"(?<=\d)[^\S\r\n]*\*[^\S\r\n]*(?=\d)", " умножить на ", text)
    text = re.sub(r"\*+", " ", text)
    for character, replacement in {"№": " номер ", "%": " процентов ", "°": " градусов ", "±": " плюс минус ",
                                   "₽": " рублей ", "$": " долларов ", "€": " евро ", "&": " и ",
                                   "=": " равно ", "<": " меньше ", ">": " больше ", "≤": " не больше ", "≥": " не меньше "}.items():
        text = text.replace(character, replacement)
    text = re.sub(r"(?<=\d)\s*\+\s*(?=\d)", " плюс ", text)
    text = re.sub(r"(?<!\w)\+(?=\d)", "плюс ", text)
    text = re.sub(r"(?<=[а-яёА-ЯЁ])\+(?=[а-яёА-ЯЁ])", "", text)
    def roman_heading(match):
        roman = match.group(2).upper()
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        total = sum(-values[value] if position + 1 < len(roman) and values[value] < values[roman[position + 1]] else values[value]
                    for position, value in enumerate(roman))
        return match.group(1) + " " + integer_words(str(total)) if 0 < total <= 3999 else match.group()
    text = re.sub(r"\b(глава|часть|раздел|том)\s+([IVXLCDM]{1,8})(?![A-Za-z])", roman_heading, text, flags=re.I)
    text = verbalize_numbers(text)
    text = _CONSONANT_ABBREVIATION.sub(
        lambda match: " ".join(_LETTER_NAMES[character.lower()] for character in match.group()), text)
    # Russian Silero filters Latin letters out. A deterministic transliteration
    # retains foreign words instead of turning their whole fragment into empty input.
    text = re.sub(r"[A-Za-z]+", lambda match: "".join(_LATIN[character] for character in match.group().lower()), text)
    text = "".join(" " + _GREEK[character.lower()] + " " if character.lower() in _GREEK else character for character in text)
    text = (_UNSPOKEN_MARKED if preserve_stress else _UNSPOKEN).sub(" ", text)
    if preserve_stress:
        text = re.sub(r"([аеёиоуыэюяАЕЁИОУЫЭЮЯ])\ue000", r"+\1", text).replace("\ue000", "")
    text = re.sub(r"[—–]+", ", ", text)
    text = re.sub(r"[.!?]{2,}", ".", text)
    text = re.sub(r"[ \t]+([.,!?;:])", r"\1", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def has_speech(text):
    return any(character.isalnum() for character in text)


def synthesis_error_reason(error):
    message = str(error).strip() or "модель вернула исключение без сообщения"
    trace = error.__traceback__
    while trace and trace.tb_next:
        trace = trace.tb_next
    location = f"; {trace.tb_frame.f_code.co_name}, строка {trace.tb_lineno}" if trace else ""
    return f"{type(error).__name__}: {message}{location}"


def plain_fallback(text, *, prepared=False):
    # Keep all letters/numbers; simplify only punctuation and control syntax.
    return " ".join("".join(character if character.isalnum() or character in _PLAIN_PUNCTUATION
                            else " " for character in (text if prepared else prepare_text(text))).split())


def is_markup_error(error):
    message = str(error).casefold()
    return any(marker in message for marker in (
        "некорректная разметка", "эмфаз", "invalid emphasis", "focus markup",
        "invalid ssml", "invalid xml", "failed to parse ssml", "failed to process ssml"))


def synthesize_text(generate, text, *, prepared=False):
    """At most one changed-input retry, only for known text markup errors.

    OOM, CUDA and unrelated failures propagate. No meaningful passage is
    discarded, and the same failing input is never retried in a loop.
    """
    prepared = text if prepared else prepare_text(text)
    validate_speech_text(prepared)
    try:
        return generate(prepared), False
    except (ValueError, AssertionError) as error:
        if not is_markup_error(error):
            raise
        fallback = plain_fallback(prepared, prepared=True)
        if fallback == prepared:
            raise TextSynthesisError("Модель отклонила обычный текст после очистки разметки.") from error
        validate_speech_text(fallback)
        return generate(fallback), True
