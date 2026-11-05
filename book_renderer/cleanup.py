"""Conservative, inspectable removal of non-narrative material in paged books."""
from collections import Counter
import math
import re
import unicodedata

from .speech import normalize_ocr_letters


VERSION = 2
_INVISIBLE = str.maketrans("", "", "\u00ad\u061c\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff")
_RUNNING_HEADING = re.compile(r"^(?:(?:прелюдия|пролог|эпилог|введение|заключение)\s*[—–-]|"
                              r"(?:часть|глава|раздел)\s+(?:[IVXLCDM]+|\d+)\s*[—–-])\s*\S", re.I)
PAGE_NUMBER = re.compile(r"^[\s—–\-·]*(?:(?:стр(?:аница)?\.?|с\.)\s*)?\d{1,4}[\s—–\-·]*$", re.I)
LEADER_ENTRY = re.compile(r"[.·…_]{2,}\s*\d{1,4}\s*$")
NUMBER_ENTRY = re.compile(r"[а-яёa-z].{2,145}\s+\d{1,4}\s*$", re.I)
CITATION = re.compile(r"^(?:\[\d{1,3}\]|\d{1,3}[.)])\s+\S|\b(?:ISBN|DOI)\b|https?://", re.I)
CATALOG = re.compile(r"^(?:ISBN\b|ISSN\b|УДК\s*\d|ББК\s*\d|©|Copyright\b)", re.I)
IMPRINT = re.compile(r"^(?:подписано\s+в\s+печать|формат\s+\d|тираж\s+\d|заказ\s*[№N]\s*\d|"
                     r"отпечатано\s+в|все\s+права\s+защищены|all\s+rights\s+reserved)", re.I)
WATERMARK = re.compile(r"^(?:скача(?:но|ть)\s+(?:с|на)\s+|(?:бесплатные|скачать)\s+(?:электронные\s+)?книги\b)", re.I)


def label(text):
    return re.sub(r"[\s\W_]+", "", text.casefold())


def toc_heading(text):
    return label(text) in {"оглавление", "содержание", "краткоесодержание"}


def toc_entry(text):
    return bool(LEADER_ENTRY.search(text) or (len(text) <= 150 and NUMBER_ENTRY.search(text)
                                             and not re.search(r"[!?]\s|[.!?]$", text)))


def visible_line(line):
    line = normalize_ocr_letters(unicodedata.normalize("NFC", line.translate(_INVISIBLE)))
    return re.sub(r"[^\S\n]+", " ", line).strip()


def running_headers(blocks, titles=()):
    """Learn complete short headings; never guess a prefix out of prose."""
    candidates = Counter()
    for lines in blocks:
        candidates.update(line for line in lines if 10 <= len(line) <= 120
                          and _RUNNING_HEADING.match(line) and not re.search(r"[.!?]$", line))
    result = {line for line, count in candidates.items() if count >= 3}
    result.update(visible_line(title) for title in titles
                  if 10 <= len(title) <= 120 and _RUNNING_HEADING.match(visible_line(title)))
    return sorted(result, key=len, reverse=True)


def split_running_header(line, headers):
    for header in headers:
        if line.casefold() == header.casefold():
            return header, ""
        if line.casefold().startswith(header.casefold() + " "):
            rest = line[len(header):].lstrip()
            if len(rest) >= 20 and any(character.isalpha() for character in rest):
                return line[:len(header)], rest
    return None, line


def clean_document_noise(document, force=False):
    """Clean existing chapters without OCR, keeping their identities and report."""
    chapters = document["chapters"]
    blocks = [[visible_line(line) for line in chapter["text"].splitlines()] for chapter in chapters]
    options = document.get("import_options", {})
    remove_numbers = force or options.get("clean_noise", True)
    remove_headers = force or options.get("remove_headers", True)
    paged = "ocr_pages" in document
    headers = running_headers(blocks, [chapter["title"] for chapter in chapters]) if remove_headers else []
    seen = set()
    excluded = document.setdefault("excluded", [])
    removed_pages = removed_headers = 0
    for chapter, lines in zip(chapters, blocks):
        original_lines = chapter["text"].splitlines()
        meaningful = [index for index, line in enumerate(lines) if line]
        edges = set(meaningful[:2] + meaningful[-2:])
        prose = sum(character.isalpha() for line in lines for character in line) >= 150
        kept = []
        changes = []
        for index, line in enumerate(lines):
            reason = None
            removed_text = line
            if paged and remove_numbers and prose and index in edges and PAGE_NUMBER.fullmatch(line):
                digits = re.search(r"\d+", line).group()
                explicit = bool(re.search(r"стр|с\.", line, re.I))
                # A bare four-digit year is ambiguous after page boundaries
                # have been lost. Keep it unless explicitly labelled a page.
                if explicit or len(digits) <= 3 and index in set(meaningful[-2:]):
                    reason = "Номер страницы"
            header, rest = split_running_header(line, headers)
            if reason is None and header:
                key = header.casefold()
                if rest or key in seen:
                    reason = "Повторяющийся колонтитул"
                    removed_text, line = header, rest
                seen.add(key)
            if reason:
                if reason == "Номер страницы":
                    line = ""
                changes.append({"chapter": chapter["index"], "line": index + 1,
                                "text": original_lines[index] if reason == "Номер страницы" else removed_text, "reason": reason})
            kept.append(line)
        text = "\n".join(kept).strip()
        if not any(character.isalpha() for character in text):
            continue  # Do not turn a legitimate number-only section into silence.
        chapter["text"] = text
        excluded.extend(changes)
        removed_pages += sum(change["reason"] == "Номер страницы" for change in changes)
        removed_headers += sum(change["reason"] == "Повторяющийся колонтитул" for change in changes)
    cleanup = document.setdefault("cleanup", {})
    cleanup["version"] = max(cleanup.get("version", 1), 3)
    cleanup["removed_lines"] = cleanup.get("removed_lines", 0) + removed_pages + removed_headers
    cleanup["removed_page_numbers"] = cleanup.get("removed_page_numbers", 0) + removed_pages
    cleanup["removed_running_headers"] = cleanup.get("removed_running_headers", 0) + removed_headers
    reasons = cleanup.setdefault("reasons", {})
    for reason, count in (("Номер страницы", removed_pages), ("Повторяющийся колонтитул", removed_headers)):
        if count:
            reasons[reason] = reasons.get(reason, 0) + count
    return {"removed_page_numbers": removed_pages, "removed_running_headers": removed_headers}


def clean_pages(pages, options):
    """Return cleaned lines plus every exclusion and aggregate counts.

    No persistent originals are modified. Reimport with cleanup switched off
    recovers material. Whole-page removal requires several independent signals;
    a lone heading or prose mentioning a table of contents is retained.
    """
    pages = [[visible_line(line) for line in lines] for lines in pages]
    removed = []
    reasons = Counter()
    total = len(pages)
    normalize = lambda line: re.sub(r"\d+", "#", line.casefold())
    repeated = Counter()
    if options.get("remove_headers", True) and total >= 3:
        for lines in pages:
            repeated.update({normalize(line) for line in lines[:2] + lines[-2:] if 2 <= len(line) <= 120
                             and not re.match(r"^(?:глава|часть|раздел|приложение)(?:\s|[.:]|$)", line, re.I)
                             and not toc_heading(line)})
    headers = {text for text, count in repeated.items() if count >= max(3, math.ceil(total * 0.5))}
    prefixes = running_headers([lines[:2] + lines[-2:] for lines in pages]) if options.get("remove_headers", True) else []
    seen_prefixes = set()
    result = []
    previous_toc = False
    references = False

    def exclude(page, index, text, reason):
        removed.append({"page": page, "line": index + 1, "text": text, "reason": reason})
        reasons[reason] += 1

    for number, lines in enumerate(pages, 1):
        edge_indices = set(range(min(2, len(lines)))) | set(range(max(0, len(lines) - 2), len(lines)))
        decisions = {}
        if options.get("remove_headers", True):
            for index in sorted(edge_indices):
                if normalize(lines[index]) in headers:
                    decisions[index] = "Повторяющийся колонтитул"
                header, rest = split_running_header(lines[index], prefixes)
                if header:
                    key = header.casefold()
                    if rest:
                        exclude(number, index, header, "Повторяющийся колонтитул")
                        lines[index] = rest
                        decisions.pop(index, None)
                    elif key in seen_prefixes:
                        decisions[index] = "Повторяющийся колонтитул"
                    else:
                        decisions.pop(index, None)  # Keep the first complete section heading.
                    seen_prefixes.add(key)

        if options.get("clean_noise", True):
            # Marginal page numbers/decorations are distinct from numbers in prose.
            for index, line in enumerate(lines):
                if index in edge_indices and PAGE_NUMBER.fullmatch(line):
                    decisions[index] = "Номер страницы"
                elif re.fullmatch(r"[—–\-=_*•·.…~|\s]{3,}", line):
                    decisions[index] = "Декоративная строка"
                elif len(line) <= 180 and CATALOG.search(line):
                    decisions[index] = "Библиографический код / права"
                elif len(line) <= 180 and (number <= 8 or number > total - 6) and IMPRINT.search(line):
                    decisions[index] = "Издательские сведения"
                elif len(line) <= 180 and WATERMARK.search(line):
                    decisions[index] = "Рекламная строка сканирования"

            active = [(index, line) for index, line in enumerate(lines) if index not in decisions]
            entries = sum(toc_entry(line) for _, line in active)
            heading_indices = [index for index, line in active if toc_heading(line)]
            near_book_edge = number <= max(8, math.ceil(total * 0.15)) or number > total * 0.85
            # Density is a clue to seek a block, never permission to discard
            # every other line: prose can start on the same contents page.
            leaders = sum(bool(LEADER_ENTRY.search(line)) for _, line in active)
            implicit_toc = (entries >= 4 and entries / max(len(active), 1) >= 0.6
                            and (near_book_edge or previous_toc)
                            and (leaders >= 2 or previous_toc))
            starts = [(index, True) for index in heading_indices]
            if not starts and implicit_toc and active:
                starts = [(active[0][0], False)]
            found_toc = False
            for start, explicit in starts:
                block = [start] if explicit else []
                cursor = start + 1 if explicit else start
                while cursor < len(lines):
                    if cursor in decisions or toc_entry(lines[cursor]):
                        block.append(cursor)
                    elif (len(lines[cursor]) <= 100 and cursor + 1 < len(lines)
                          and toc_entry(lines[cursor + 1]) and not re.search(r"[.!?]$", lines[cursor])):
                        block.append(cursor)  # Wrapped title before a page reference.
                    else:
                        break
                    cursor += 1
                if sum(toc_entry(lines[index]) for index in block) >= (2 if explicit else 4):
                    found_toc = True
                    for index in block:
                        if index not in decisions:
                            decisions[index] = "Оглавление"
            previous_toc = found_toc

            # Reference lists are optional: short quotations/footnotes are kept.
            if options.get("skip_references", True) and number >= total * 0.7:
                reference_heading = any(label(line) in {"списоклитературы", "библиография", "литература",
                                                        "использованнаялитература", "списокисточников"}
                                        for _, line in active[:3])
                citations = sum(bool(CITATION.search(line)) for _, line in active)
                dense = citations >= 3 and citations / max(len(active), 1) >= 0.4
                if dense and (reference_heading or references):
                    for index, _ in active:
                        decisions[index] = "Список литературы"
                    references = True
                else:
                    references = False
        else:
            previous_toc = False
            references = False

        kept = []
        for index, line in enumerate(lines):
            if index in decisions:
                exclude(number, index, line, decisions[index])
            else:
                kept.append(line)
        result.append(kept)

    return result, removed, {"version": VERSION, "removed_lines": len(removed),
                             "removed_pages": sum(bool(before) and not after for before, after in zip(pages, result)),
                             "reasons": dict(reasons), "options": {key: options.get(key, True)
                                 for key in ("clean_noise", "remove_headers", "skip_references")}}
