from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .config import LIBRARY, WINDOWS


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Неверный идентификатор.")
    return value


def book_dir(book_id):
    return LIBRARY / "books" / identity(book_id)


def job_dir(job_id):
    return LIBRARY / "jobs" / identity(job_id)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".part")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
        if WINDOWS:
            from .platform.windows_files import replace_json
            replace_json(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Preserve the original write/replace error.


def read_json(path, maximum=64 * 1024 ** 2):
    if path.is_symlink() or path.stat().st_size > maximum:
        raise ValueError("Недопустимый размер или путь JSON.")
    if WINDOWS:
        from .platform.windows_files import shared_read
        # A normal Python read handle prevents the worker's atomic os.replace
        # while the backend polls progress.json. Keep FILE_SHARE_DELETE enabled.
        with shared_read(path) as source:
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Недопустимый размер JSON.")
        return json.loads(data.decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


class BookDocumentUnavailable(ValueError):
    """A missing book document must not prevent opening the whole library."""


def read_book_document(book):
    revision = book.get("document_revision", "")
    try:
        if not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("Неверная версия текста.")
        document = read_json(book_dir(book["id"]) / "canonical" / (revision + ".json"))
        if (not isinstance(document, dict) or document.get("schema") != "projectpro-local-book-v1"
                or not isinstance(document.get("chapters"), list) or not document["chapters"]):
            raise ValueError("Неверный формат текста.")
        return document
    except (OSError, ValueError, UnicodeError) as error:
        source = book_dir(book["id"]) / ("source." + book["format"])
        recovery = ("Оригинал сохранён: нажми «Извлечь текст заново», чтобы восстановить текст. "
                    "Прежние ручные правки можно вернуть из резервной копии."
                    if source.is_file() else
                    "Оригинал книги тоже не найден. Восстанови её каталог из резервной копии "
                    "либо удали недоступную запись в галерее и загрузи книгу заново.")
        raise BookDocumentUnavailable(
            "Подготовленный текст книги отсутствует, недоступен или повреждён. "
            "Готовые MP3 не удалялись. " + recovery) from error


@contextmanager
def database():
    connection = sqlite3.connect(LIBRARY / "library.sqlite3", timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    for directory in (LIBRARY, LIBRARY / "books", LIBRARY / "jobs", LIBRARY / "mp3"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with database() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise RuntimeError("Неизвестная версия локальной библиотеки; данные не изменены.")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS books (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL, format TEXT NOT NULL, source_sha256 TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, error TEXT, chapters INTEGER NOT NULL DEFAULT 0,
                document_revision TEXT NOT NULL DEFAULT '',
                warnings TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS renders (
                id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                settings TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(book_id, settings)
            );
            CREATE TABLE IF NOT EXISTS audio (
                render_id TEXT NOT NULL REFERENCES renders(id) ON DELETE CASCADE,
                chapter INTEGER NOT NULL, filename TEXT NOT NULL, duration REAL NOT NULL,
                sha256 TEXT NOT NULL, PRIMARY KEY(render_id, chapter)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, render_id TEXT REFERENCES renders(id) ON DELETE CASCADE,
                options TEXT NOT NULL, status TEXT NOT NULL, cancel INTEGER NOT NULL DEFAULT 0,
                progress TEXT NOT NULL DEFAULT '{}', error TEXT,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS chapter_audio (
                book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                chapter INTEGER NOT NULL, text_sha256 TEXT NOT NULL DEFAULT '',
                settings TEXT NOT NULL DEFAULT '{}', duration REAL, sha256 TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'missing', error TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(book_id, chapter)
            );
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(books)")}
        if "render_settings" not in columns:
            connection.execute("ALTER TABLE books ADD COLUMN render_settings TEXT NOT NULL DEFAULT '{}' ")
        if "audio_folder" not in columns:
            connection.execute("ALTER TABLE books ADD COLUMN audio_folder TEXT NOT NULL DEFAULT ''")
        if "custom_cover" not in columns:
            connection.execute("ALTER TABLE books ADD COLUMN custom_cover INTEGER NOT NULL DEFAULT 0")
        # No automatic restart of work interrupted by closing the local server.
        connection.execute("UPDATE jobs SET status='interrupted', error=?, finished_at=? WHERE status='running'",
                           ("Локальный сервер был остановлен. Готовые главы сохранены; запуск можно повторить.", now()))
        # Retain completed files awaiting Windows replacement across restarts.
        for row in connection.execute("SELECT id FROM jobs WHERE kind='render' AND status='interrupted'").fetchall():
            if (job_dir(row[0]) / "out/publication-pending.json").is_file():
                connection.execute("UPDATE jobs SET status='publication_pending' WHERE id=?", (row[0],))
        connection.execute("UPDATE books SET status='interrupted' WHERE status='importing'")
        connection.execute("UPDATE chapter_audio SET status='pending' WHERE status='rendering'")
        if version < 2:
            connection.execute("UPDATE jobs SET status='interrupted',error=?,finished_at=? WHERE status='queued'",
                               ("Обновлено хранение MP3 по главам. Выбери неозвученные главы и продолжи.", now()))
    from .audio import migrate_legacy_audio
    if version == 1:
        print("Обновляю библиотеку: переношу готовые MP3 в единый список глав…", flush=True)
    unavailable_books = migrate_legacy_audio()
    # Terminal job scratch is never an alternate audio library. On an
    # interrupted chapter the owner restarts it; published MP3 stay in mp3/.
    with database() as connection:
        terminal_jobs = [row["id"] for row in connection.execute(
            "SELECT id,book_id FROM jobs WHERE status NOT IN ('queued','running','publication_pending')")
            if row["book_id"] not in unavailable_books]
    import shutil
    for job_id in terminal_jobs:
        scratch = job_dir(job_id) / "out"
        if scratch.exists():
            shutil.rmtree(scratch)
    with database() as connection:
        connection.execute("PRAGMA user_version=2")


def fetch_book(book_id):
    with database() as connection:
        row = connection.execute("SELECT * FROM books WHERE id=?", (identity(book_id),)).fetchone()
    if row is None:
        raise LookupError("Книга не найдена.")
    result = dict(row)
    result["warnings"] = json.loads(result["warnings"])
    return result


def new_job(connection, book_id, kind, options, render_id=None):
    job_id = uuid4().hex
    directory = job_dir(job_id)
    directory.mkdir(mode=0o700)
    (directory / "out").mkdir(mode=0o700)
    connection.execute("INSERT INTO jobs(id,book_id,kind,render_id,options,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                       (job_id, book_id, kind, render_id, json.dumps(options, ensure_ascii=False), now()))
    if WINDOWS:
        from .diagnostics import record
        record("job_queued", destination=directory / "manager.log", job_id=job_id, kind=kind)
    return job_id
