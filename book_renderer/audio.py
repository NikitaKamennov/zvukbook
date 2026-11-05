"""One current MP3 per chapter, independent of whole-document revisions."""
import hashlib
import json
import os
import re
import shutil
import unicodedata
from uuid import uuid4

from .config import DISK_RESERVE, LIBRARY
from .storage import BookDocumentUnavailable, book_dir, database, digest, identity, now, read_book_document, read_json
from .pronunciation import profile_for, speech_fingerprint


def readable_audio_name(title, author, suffix):
    """A single portable filename component, including on Windows."""
    title = " ".join(str(title).split()) or "Книга"
    author = " ".join(str(author).split())
    # Keep the title first, so a long author name cannot hide the book name.
    value = title[:65] + (" — " + author[:30] if author else "")
    value = unicodedata.normalize("NFC", value)
    value = "".join(" " if character in '<>:"/\\|?*' or unicodedata.category(character).startswith("C")
                    else character for character in value)
    value = " ".join(value.split()).strip(" .") or "Книга"
    # Bound UTF-8 bytes as well as Windows characters (also safe on ext4).
    value = value.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    return value + " [" + suffix + "]"


def assign_audio_folder(connection, book_id, document):
    """Pick a stable human-readable folder when a new book is first imported."""
    book_id = identity(book_id)
    row = connection.execute("SELECT audio_folder FROM books WHERE id=?", (book_id,)).fetchone()
    if row is None:
        raise LookupError("Книга не найдена.")
    if row[0]:
        return
    # Existing test libraries need no bulk move. Keep their old files in place.
    legacy = book_dir(book_id) / "mp3"
    if any(legacy.glob("*.mp3")) or (legacy / "cover.jpg").is_file():
        return
    for length in (8, 12, 16, 32):
        name = readable_audio_name(document["title"], document.get("author", ""), book_id[:length])
        claimed = connection.execute("SELECT 1 FROM books WHERE audio_folder=? AND id<>?", (name, book_id)).fetchone()
        if not claimed and not (LIBRARY / "mp3" / name).exists():
            connection.execute("UPDATE books SET audio_folder=? WHERE id=?", (name, book_id))
            return
    raise ValueError("Не удалось выбрать свободную папку MP3. Существующие файлы не изменены.")


def audio_dir(book_id, connection=None):
    book_id = identity(book_id)
    if connection is None:
        with database() as connection:
            return audio_dir(book_id, connection)
    row = connection.execute("SELECT audio_folder FROM books WHERE id=?", (book_id,)).fetchone()
    if row is None:
        raise LookupError("Книга не найдена.")
    name = row[0]
    if not name:
        return book_dir(book_id) / "mp3"
    suffix = re.search(r" \[([a-f0-9]{8,32})\]$", name)
    if (name in (".", "..") or any(character in name for character in '/\\:')
            or not suffix or not book_id.startswith(suffix.group(1))):
        raise ValueError("Неверная папка MP3 в библиотеке.")
    path = LIBRARY / "mp3" / name
    if path.resolve().parent != (LIBRARY / "mp3").resolve():
        raise ValueError("Папка MP3 должна находиться в локальной библиотеке.")
    return path


def audio_path(book_id, index):
    if type(index) is not int or not 0 <= index <= 2000:
        raise ValueError("Неверный номер главы.")
    return audio_dir(book_id) / f"{index:04d}.mp3"


def chapter_hash(chapter, document, profile=None):
    value = {"text": chapter["text"], "title": chapter["title"],
             "remove_editor_notes": document.get("import_options", {}).get("remove_editor_notes", True)}
    base_hash = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    try:
        effective = profile_for(document, chapter.get("index")) if document.get("pronunciation_applied_chapters") else (profile or profile_for(document))
        effect = speech_fingerprint(base_hash, chapter["text"], document, effective,
                                    chapter.get("index") in document.get("pronunciation_applied_chapters", []))
    except ValueError as error:
        raise ValueError(f"Глава {chapter.get('index', '?')}: {error}") from error
    return hashlib.sha256((base_hash + "/" + effect).encode()).hexdigest() if effect else base_hash


def sync_chapters(connection, book_id, document):
    """Keep unchanged chapters ready; only edited chapters become stale."""
    chapters = {chapter["index"]: chapter for chapter in document["chapters"]}
    existing = {row["chapter"]: dict(row) for row in connection.execute(
        "SELECT * FROM chapter_audio WHERE book_id=?", (book_id,))}
    if 0 in existing:
        chapters[0] = document["chapters"][0]
    directory = audio_dir(book_id, connection)
    profile = profile_for(document)
    for index, chapter in chapters.items():
        expected = chapter_hash(chapter, document, profile)
        old = existing.get(index)
        if old is None:
            connection.execute("INSERT INTO chapter_audio(book_id,chapter,status,updated_at) VALUES(?,?,'missing',?)",
                               (book_id, index, now()))
        else:
            present = (directory / f"{index:04d}.mp3").is_file()
            status = old["status"]
            error = old["error"]
            if not present:
                status, error = "missing", None
            elif old["text_sha256"] != expected:
                status, error = "stale", None
            elif status in ("missing", "stale"):
                status, error = "ready", None
            connection.execute("UPDATE chapter_audio SET status=?,error=? WHERE book_id=? AND chapter=?",
                               (status, error, book_id, index))
    connection.execute("DELETE FROM chapter_audio WHERE book_id=? AND chapter>?", (book_id, len(document["chapters"])))


def chapter_records(book_id, document):
    with database() as connection:
        rows = {row["chapter"]: dict(row) for row in connection.execute(
            "SELECT * FROM chapter_audio WHERE book_id=?", (book_id,))}
    directory = audio_dir(book_id)
    profile = profile_for(document)
    result = []
    for chapter in document["chapters"]:
        row = rows.get(chapter["index"], {})
        path = directory / f"{chapter['index']:04d}.mp3"
        present = path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        same_text = row.get("text_sha256") == chapter_hash(chapter, document, profile)
        status = row.get("status", "missing")
        if status not in ("pending", "rendering", "failed"):
            status = "missing" if not present else "stale" if not same_text else status
        ready = present and same_text and status == "ready"
        result.append({"index": chapter["index"], "title": chapter["title"], "characters": len(chapter["text"]),
                       "available": present, "ready": ready, "duration": row.get("duration"),
                       "sha256": row.get("sha256", ""), "settings": json.loads(row.get("settings", "{}")),
                       "status": status,
                       "error": row.get("error")})
    return result


def prune_audio(book_id):
    directory = audio_dir(book_id)
    if not directory.exists():
        return
    with database() as connection:
        wanted = {f"{row[0]:04d}.mp3" for row in connection.execute(
            "SELECT chapter FROM chapter_audio WHERE book_id=?", (book_id,))}
    for path in directory.iterdir():
        if re.fullmatch(r"\d{4}\.mp3", path.name) and path.name not in wanted:
            path.unlink()


def migrate_legacy_audio():
    """Run on owner startup. Copy/verify current files before removing archives.

    Interrupted migrations can resume: chapter_audio rows/files are preferred
    over legacy sources already copied during a previous startup.
    """
    with database() as connection:
        books = [dict(row) for row in connection.execute("SELECT * FROM books")]
    unavailable = set()
    for book in books:
        if not book["document_revision"]:
            continue
        canonical = book_dir(book["id"]) / "canonical"
        try:
            document = read_book_document(book)
        except BookDocumentUnavailable as error:
            unavailable.add(book["id"])
            with database() as connection:
                connection.execute("UPDATE books SET status='failed',error=? WHERE id=?", (str(error), book["id"]))
                connection.execute("""UPDATE jobs SET status='interrupted',error=?,finished_at=?
                    WHERE book_id=? AND status='queued'""", (str(error), now(), book["id"]))
            # Preserve legacy MP3, DB references and task scratch for recovery.
            continue
        profile = profile_for(document)
        expected = {chapter["index"]: chapter_hash(chapter, document, profile) for chapter in document["chapters"]}
        expected[0] = chapter_hash(document["chapters"][0], document, profile)
        by_hash = {}
        for index, sha in expected.items():
            if index:
                by_hash.setdefault(sha, []).append(index)
        with database() as connection:
            renders = [dict(row) for row in connection.execute(
                "SELECT * FROM renders WHERE book_id=? ORDER BY created_at DESC", (book["id"],))]
            old_audio = [dict(row) for row in connection.execute(
                "SELECT a.* FROM audio a JOIN renders r ON r.id=a.render_id WHERE r.book_id=?", (book["id"],))]
            existing = {row["chapter"]: dict(row) for row in connection.execute(
                "SELECT * FROM chapter_audio WHERE book_id=?", (book["id"],))}
        candidates = {}
        for render in renders:
            identity(render["id"])
            settings = json.loads(render["settings"])
            revision = settings.get("document_revision", "")
            if not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise ValueError("Неверная версия текста при переносе MP3.")
            try:
                source = read_json(canonical / (revision + ".json"))
            except (OSError, ValueError):
                source = None
            source_chapters = {chapter["index"]: chapter for chapter in source["chapters"]} if source else {}
            if source:
                source_chapters[0] = source["chapters"][0]
            for row in old_audio:
                if row["render_id"] != render["id"]:
                    continue
                index = row["chapter"]
                path = book_dir(book["id"]) / "renders" / render["id"] / f"{index:04d}.mp3"
                if path.is_symlink() or not path.is_file():
                    continue
                actual_sha = digest(path)
                verified = actual_sha == row["sha256"]
                sha = chapter_hash(source_chapters[index], source) if verified and index in source_chapters else ""
                row = dict(row, sha256=actual_sha)
                target = index if index in expected else None
                if index and expected.get(index) != sha and len(by_hash.get(sha, [])) == 1:
                    target = by_hash[sha][0]
                if target is None:
                    continue
                exact = bool(sha) and sha == expected[target]
                if target not in candidates or exact and not candidates[target][0]:
                    candidates[target] = (exact, path, row, sha, settings)
        directory = audio_dir(book["id"])
        directory.mkdir(mode=0o700, exist_ok=True)
        for index, (exact, path, row, sha, settings) in candidates.items():
            current = existing.get(index)
            target = audio_path(book["id"], index)
            if (current and target.is_file() and not target.is_symlink()
                    and digest(target) == current["sha256"]
                    and (current["text_sha256"] == expected[index] or not exact)):
                continue
            if shutil.disk_usage(directory).free < DISK_RESERVE + path.stat().st_size:
                raise ValueError("Для переноса MP3 нужно место для копии файлов и резерв 5 GiB. Прежние файлы не удалены.")
            partial = directory / (f"{index:04d}.mp3.part-" + uuid4().hex)
            try:
                shutil.copyfile(path, partial)
                if digest(partial) != row["sha256"]:
                    raise ValueError("Контрольная сумма копии MP3 не совпала; прежний файл сохранён.")
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
            settings = {key: value for key, value in settings.items() if key not in ("document_revision", "generation")}
            with database() as connection:
                connection.execute("""INSERT OR REPLACE INTO chapter_audio
                    (book_id,chapter,text_sha256,settings,duration,sha256,status,error,updated_at)
                    VALUES(?,?,?,?,?,?,?,NULL,?)""", (book["id"], index, sha, json.dumps(settings, ensure_ascii=False),
                        row["duration"], row["sha256"], "ready" if exact else "stale", now()))
        with database() as connection:
            sync_chapters(connection, book["id"], document)
            if (book["status"] == "failed" and (book.get("error") or "").startswith(
                    "Подготовленный текст книги отсутствует, недоступен или повреждён.")):
                connection.execute("UPDATE books SET status='ready',error=NULL WHERE id=?", (book["id"],))
            if renders and book["render_settings"] == "{}":
                settings = json.loads(renders[0]["settings"])
                selected = {key: settings[key] for key in ("voice", "rate", "pitch")}
                connection.execute("UPDATE books SET render_settings=? WHERE id=?",
                                   (json.dumps(selected, ensure_ascii=False), book["id"]))
            connection.execute("UPDATE jobs SET render_id=NULL WHERE book_id=?", (book["id"],))
            connection.execute("DELETE FROM renders WHERE book_id=?", (book["id"],))
        legacy = book_dir(book["id"]) / "renders"
        if legacy.is_symlink():
            legacy.unlink()
        elif legacy.exists():
            shutil.rmtree(legacy)
        prune_audio(book["id"])
        for partial in directory.glob("*.mp3.part-*"):
            if partial.is_file() or partial.is_symlink():
                partial.unlink()
    return unavailable
