"""Remove identifiable editorial footnotes while retaining an exclusion report."""
import re


VERSION = 1
STARS = "*＊﹡∗⁎✱"
_STAR_RUN = re.compile("[" + re.escape(STARS) + "]+")
_NOTE_START = re.compile(r"(?<![\w" + re.escape(STARS) + r"])[" + re.escape(STARS)
                         + r"]{1,5}[ \t]*(?=[А-ЯЁA-Z«„“\"(])")
_NOTE_END = re.compile(r"(?:[—–-][ \t]*)?\bПрим(?:ечание)?[.,]?[ \t]*"
                       r"(?:науч(?:ного)?[.,]?[ \t]*)?"
                       r"(?:ред(?:актора)?|пер(?:еводчика)?)(?![а-яё])[.,]?", re.I)
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
MAX_NOTE = 4000


def strip_markers(text):
    count = 0

    def replace(match):
        nonlocal count
        before, after = match.start() - 1, match.end()
        while before >= 0 and text[before] in " \t":
            before -= 1
        while after < len(text) and text[after] in " \t":
            after += 1
        left = text[before] if before >= 0 else ""
        right = text[after] if after < len(text) else ""
        # Multiplication/exponent syntax is preserved in the displayed text;
        # speech.prepare_text turns numeric operators into spoken words later.
        if left.isdigit() and right.isdigit() and len(match.group()) <= 2:
            return match.group()
        count += 1
        if left and (left.isalnum() or left in "»”\"')]") and (not right or right.isspace() or right in ",.;:!?…»”\"')]"):
            return ""
        return " "

    cleaned = _STAR_RUN.sub(replace, text)
    cleaned = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines()).strip(), count


def clean_editor_notes(text, remove_notes=True):
    """Return narration, exact removed note spans, and marker count.

    Only star-led notes with an explicit editor/translator signature qualify.
    Search is bounded to one paragraph and MAX_NOTE characters; select the
    nearest preceding note start, not an earlier reference in the main text.
    """
    spans = []
    if remove_notes:
        boundaries = list(_PARAGRAPH_BREAK.finditer(text))
        paragraph_start = 0
        for boundary in [*boundaries, None]:
            paragraph_end = boundary.start() if boundary is not None else len(text)
            paragraph = text[paragraph_start:paragraph_end]
            starts = iter(_NOTE_START.finditer(paragraph))
            next_start = next(starts, None)
            latest = None
            local_spans = []
            for ending in _NOTE_END.finditer(paragraph):
                while next_start is not None and next_start.end() <= ending.start():
                    latest = next_start
                    next_start = next(starts, None)
                if latest is not None and ending.end() - latest.start() <= MAX_NOTE:
                    local_spans.append([latest.start(), ending.end()])
                    latest = None
                elif local_spans and ending.end() - local_spans[-1][0] <= MAX_NOTE:
                    # One starred note may contain translator + editor comments
                    # with two signatures but no second star (a common OCR case).
                    local_spans[-1][1] = ending.end()
            spans.extend((paragraph_start + start, paragraph_start + end) for start, end in local_spans)
            if boundary is not None:
                paragraph_start = boundary.end()
    removed = [{"offset_start": start, "offset_end": end, "text": text[start:end],
                "reason": "Примечание редактора/переводчика"} for start, end in spans]
    parts = []
    position = 0
    for start, end in spans:
        parts.extend((text[position:start], " "))
        position = end
    parts.append(text[position:])
    cleaned, markers = strip_markers("".join(parts))
    return cleaned, removed, markers


def clean_document_notes(document, remove_notes=True):
    """Update a loaded document; the caller publishes a new immutable revision."""
    chapters = []
    excluded = list(document.get("excluded", []))
    notes_count = markers_count = empty_chapters = 0
    for index, chapter in enumerate(document["chapters"], 1):
        cleaned, notes, markers = clean_editor_notes(chapter["text"], remove_notes)
        notes_count += len(notes)
        markers_count += markers
        excluded.extend(dict(note, chapter=chapter.get("index", index)) for note in notes)
        if not any(character.isalnum() for character in cleaned):
            empty_chapters += 1
            continue
        chapters.append(dict(chapter, index=len(chapters) + 1, text=cleaned))
    if not chapters:
        raise ValueError("После очистки не осталось текста книги. Отключи удаление примечаний и проверь оригинал.")
    document["chapters"] = chapters
    document["excluded"] = excluded
    cleanup = document.setdefault("cleanup", {})
    cleanup["version"] = max(cleanup.get("version", 1), 2)
    cleanup["removed_notes"] = cleanup.get("removed_notes", 0) + notes_count
    cleanup["removed_markers"] = cleanup.get("removed_markers", 0) + markers_count
    cleanup.setdefault("reasons", {})
    if notes_count:
        reason = "Примечание редактора/переводчика"
        cleanup["reasons"][reason] = cleanup["reasons"].get(reason, 0) + notes_count
    document["notes_cleanup_version"] = VERSION
    document.setdefault("import_options", {})["remove_editor_notes"] = remove_notes
    if notes_count or empty_chapters:
        document.setdefault("warnings", []).append(
            f"Убрано примечаний редактора/переводчика: {notes_count}; разделов без основного текста: {empty_chapters}. "
            "Исключённые примечания сохранены в отчёте. Проверь текст перед озвучкой.")
    return {"removed_notes": notes_count, "removed_markers": markers_count, "removed_chapters": empty_chapters}
