"""Book-local pronunciation rules and explicit edits to chapter text."""
from collections import OrderedDict
from bisect import bisect_left, bisect_right
from functools import lru_cache
import hashlib
import json
import re
import threading
import unicodedata

from .notes import clean_editor_notes
from .speech import prepare_text


MAX_RULES = 300
MAX_NARRATION = 2_000_000
_VOWELS = frozenset("аеёиоуыэюя")
_WORDS = re.compile(r"[а-яёА-ЯЁ]+")
_FINGERPRINTS = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _key(value):
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def normalize_stress(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise ValueError("Укажи одно русское слово с ударением, до 80 знаков.")
    value = unicodedata.normalize("NFC", value.strip())
    if not re.fullmatch(r"[а-яёА-ЯЁ+\u0301]+", value):
        raise ValueError("Для ударения укажи одно русское слово: например, злодЕй или злоде́й.")
    letters, positions = [], set()
    explicit_mark = "+" in value or "\u0301" in value
    for index, character in enumerate(value):
        if character == "+":
            if index + 1 >= len(value) or value[index + 1].casefold() not in _VOWELS:
                raise ValueError("Знак + ставится непосредственно перед ударной гласной.")
            positions.add(len(letters))
        elif character == "\u0301":
            if not letters or letters[-1] not in _VOWELS:
                raise ValueError("Знак ударения ставится после гласной.")
            positions.add(len(letters) - 1)
        else:
            if not explicit_mark and character.isupper() and character.casefold() in _VOWELS:
                positions.add(len(letters))
            letters.append(character.casefold())
    if len(positions) != 1:
        raise ValueError("Отметь ровно одну ударную гласную: злодЕй.")
    position = next(iter(positions))
    word = "".join(letters)
    return word[:position] + "+" + word[position:]


def validate_rules(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"replacements", "stresses"}:
        raise ValueError("Некорректные правила произношения.")
    replacements, stresses = value.get("replacements", []), value.get("stresses", [])
    if not isinstance(replacements, list) or not isinstance(stresses, list) or max(len(replacements), len(stresses)) > MAX_RULES:
        raise ValueError("Допускается до 300 замен и 300 ударений на книгу.")
    result, seen = [], set()
    for rule in replacements:
        if not isinstance(rule, dict) or set(rule) != {"from", "to"}:
            raise ValueError("У каждой замены должны быть поля «Что» и «На что».")
        source, target = rule["from"], rule["to"]
        if not isinstance(source, str) or not isinstance(target, str) or not 1 <= len(source.strip()) <= 160 or len(target) > 500:
            raise ValueError("Замена: до 160 знаков поиска и до 500 знаков результата.")
        if any(unicodedata.category(ch).startswith("C") and ch not in "\t\n\r" for ch in source + target):
            raise ValueError("В правилах замены есть скрытые управляющие символы.")
        source, target = (unicodedata.normalize("NFC", " ".join(part.split())) for part in (source, target))
        if _key(source) in seen:
            raise ValueError("Одинаковое правило поиска добавлено несколько раз.")
        seen.add(_key(source))
        result.append({"from": source, "to": target})
    marked, seen = [], set()
    for value in stresses:
        accented = normalize_stress(value)
        word = accented.replace("+", "")
        if word in seen:
            raise ValueError("Для одного слова можно сохранить только одно ударение.")
        seen.add(word)
        marked.append(accented)
    return {"replacements": result, "stresses": marked}


class PronunciationProfile:
    def __init__(self, encoded):
        self.key = hashlib.sha256(encoded.encode()).hexdigest()
        self.rules = json.loads(encoded)
        self.replacements = {_key(rule["from"]): (index, rule["to"]) for index, rule in enumerate(self.rules["replacements"])}
        patterns = [re.escape(rule["from"]).replace(r"\ ", r"\s+")
                    for rule in sorted(self.rules["replacements"], key=lambda item: len(item["from"]), reverse=True)]
        self.pattern = re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.I) if patterns else None
        self.stresses = {word.replace("+", ""): (index, word.index("+")) for index, word in enumerate(self.rules["stresses"])}
        self.active = bool(self.pattern or self.stresses)

    def replace(self, text, stats=None):
        if not self.pattern:
            return text
        change = 0
        def replacement(match):
            nonlocal change
            value = self.replacements.get(_key(match.group()))
            if value is None:
                return match.group()
            index, target = value
            change += len(target) - len(match.group())
            if len(text) + change > MAX_NARRATION:
                raise ValueError("Замены слишком увеличивают главу. Сократи правило или раздели главу.")
            if stats is not None:
                stats["replacements"][index] += 1
            return target
        # One pass prevents a replacement from triggering another replacement.
        return self.pattern.sub(replacement, unicodedata.normalize("NFC", text))

    def stress(self, text, stats=None):
        if not self.stresses:
            return text
        def replacement(match):
            value = self.stresses.get(match.group().casefold())
            if value is None:
                return match.group()
            index, position = value
            if stats is not None:
                stats["stresses"][index] += 1
            word = match.group()
            return word[:position] + "+" + word[position:]
        # A stress already written in the text takes priority over old live rules.
        return re.sub(r"[а-яёА-ЯЁ+]+", lambda match: match.group() if "+" in match.group() else replacement(match), text)

    def prepare(self, text, stats=None, preserve_stress=False):
        prepared = self.stress(prepare_text(self.replace(text, stats), preserve_stress=preserve_stress), stats)
        if len(prepared) > MAX_NARRATION:
            raise ValueError("Подготовленный текст главы превышает 2 млн знаков. Раздели главу.")
        return prepared


@lru_cache(maxsize=32)
def _compile(encoded):
    return PronunciationProfile(encoded)


def profile_for(document, chapter_index=None):
    rules = validate_rules(None if chapter_index in document.get("pronunciation_applied_chapters", []) else document.get("pronunciation"))
    return _compile(json.dumps(rules, ensure_ascii=False, sort_keys=True))


def apply_to_text(text, rules, limit=20_000):
    """Apply a batch once, retaining positions in the resulting chapter."""
    profile = _compile(json.dumps(rules, ensure_ascii=False, sort_keys=True))
    events, pieces, cursor, length = [], [], 0, 0
    if profile.pattern:
        for match in profile.pattern.finditer(text):
            value = profile.replacements.get(_key(match.group()))
            if value is None or value[1] == match.group():
                continue
            prefix = text[cursor:match.start()]
            pieces.append(prefix); length += len(prefix)
            target = value[1]
            events.append({"kind": "replacement", "before": match.group(), "after": target,
                           "start": length, "end": length + len(target)})
            pieces.append(target); length += len(target); cursor = match.end()
            if len(events) > limit:
                raise ValueError("Слишком много изменений за один раз. Выбери меньше глав и повтори.")
        pieces.append(text[cursor:])
        text = "".join(pieces)

    stress_events, pieces, cursor, length = [], [], 0, 0
    words = re.compile(r"(?<![а-яёА-ЯЁ\u0301])[а-яёА-ЯЁ]+(?:\u0301[а-яёА-ЯЁ]*)?(?![а-яёА-ЯЁ\u0301])")
    for match in words.finditer(text):
        word = match.group()
        value = profile.stresses.get(word.replace("\u0301", "").casefold())
        if value is None:
            continue
        _, position = value
        plain = word.replace("\u0301", "")
        target = plain[:position + 1] + "\u0301" + plain[position + 1:]
        if target == word:
            continue
        prefix = text[cursor:match.start()]
        pieces.append(prefix); length += len(prefix)
        stress_events.append({"kind": "stress", "before": word, "after": target,
                              "start": length, "end": length + len(target),
                              "old_start": match.start(), "old_end": match.end(), "delta": len(target) - len(word)})
        pieces.append(target); length += len(target); cursor = match.end()
        if len(events) + len(stress_events) > limit:
            raise ValueError("Слишком много изменений за один раз. Выбери меньше глав и повтори.")
    pieces.append(text[cursor:])
    text = "".join(pieces)
    starts = [item["old_start"] for item in stress_events]
    ends = [item["old_end"] for item in stress_events]
    totals = [0]
    for item in stress_events:
        totals.append(totals[-1] + item["delta"])
    for event in events:
        left = bisect_right(ends, event["start"])
        right = max(left, bisect_left(starts, event["end"]))
        before = totals[left]
        inside = totals[right] - totals[left]
        event["start"] += before
        event["end"] += before + inside
    for item in stress_events:
        del item["old_start"], item["old_end"], item["delta"]
    events.extend(stress_events)
    events.sort(key=lambda item: item["start"])
    for item in events:
        item["context"] = text[max(0, item["start"] - 70):min(len(text), item["end"] + 70)]
        item["actual"] = text[item["start"]:item["end"]]
    return text, events


def prepared_chapter(text, document, profile=None, stats=None):
    narration, _, _ = clean_editor_notes(text, document.get("import_options", {}).get("remove_editor_notes", True))
    return (profile or profile_for(document)).prepare(narration, stats, preserve_stress=bool(document.get("pronunciation_materialized")))


def speech_fingerprint(base_hash, text, document, profile, preserve_stress=False):
    if not profile.active:
        return None
    key = (base_hash, profile.key, preserve_stress)
    with _CACHE_LOCK:
        if key in _FINGERPRINTS:
            _FINGERPRINTS.move_to_end(key)
            return _FINGERPRINTS[key]
    narration, _, _ = clean_editor_notes(text, document.get("import_options", {}).get("remove_editor_notes", True))
    baseline = prepare_text(narration, preserve_stress=preserve_stress)
    replaced = profile.replace(narration)
    prepared = profile.stress(baseline if replaced == narration else prepare_text(replaced, preserve_stress=preserve_stress))
    if len(prepared) > MAX_NARRATION:
        raise ValueError("Правила слишком увеличивают текст главы.")
    result = hashlib.sha256(prepared.encode()).hexdigest() if prepared != baseline else None
    with _CACHE_LOCK:
        _FINGERPRINTS[key] = result
        if len(_FINGERPRINTS) > 4096:
            _FINGERPRINTS.popitem(last=False)
    return result
