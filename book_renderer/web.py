import json
import math
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import subprocess
from uuid import uuid4
import zipfile

from flask import Flask, jsonify, render_template, request, send_file, session
import psutil
from werkzeug.exceptions import HTTPException

from .config import DISK_RESERVE, LIBRARY, MODEL_ID, MODEL_SHA256, NORMALIZER_VERSION, ROOT, SPEAKERS, UPLOAD_LIMIT, WINDOWS
from .manager import save_document
from .audio import audio_dir, audio_path, chapter_hash, chapter_records
from .pronunciation import apply_to_text, prepared_chapter, profile_for, validate_rules
from .covers import COVER_NAME, MAX_COVER_BYTES, MAX_SOURCE_BYTES, visible_cover
from .notes import clean_document_notes
from .cleanup import clean_document_noise
from .speech import VERSION as SPEECH_NORMALIZER_VERSION
from .storage import BookDocumentUnavailable, book_dir, database, digest, fetch_book, identity, job_dir, new_job, now, read_book_document, read_json


class Conflict(ValueError):
    pass


def body():
    if request.mimetype != "application/json":
        raise ValueError("Ожидается JSON.")
    def invalid(value):
        raise ValueError("Недопустимое число JSON: " + value)
    value = json.loads(request.get_data().decode("utf-8"), parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("Ожидается JSON object.")
    return value


def idle_book(connection, book_id, queue=False):
    if connection.execute("SELECT 1 FROM jobs WHERE book_id=? AND status IN ('queued','running','publication_pending')", (book_id,)).fetchone():
        raise Conflict("У этой книги уже есть задача. Дождись её или останови.")
    if queue and connection.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 16:
        raise Conflict("В очереди уже 16 задач. Дождись освобождения.")


def import_options(payload):
    options = {"encoding": payload.get("encoding", "utf-8-sig")}
    if options["encoding"] not in ("utf-8-sig", "cp1251"):
        raise ValueError("Выбери UTF-8 или Windows-1251.")
    for key, default in (("force_ocr", False), ("include_notes", False), ("remove_headers", True),
                         ("clean_noise", True), ("skip_references", True), ("remove_editor_notes", True)):
        value = payload.get(key, default)
        if type(value) is not bool:
            raise ValueError("Неверная настройка обработки текста.")
        options[key] = value
    return options


def create_app(port, limits):
    app = Flask(__name__, static_folder=str(ROOT / "book_renderer/static"),
                template_folder=str(ROOT / "book_renderer/templates"))
    app.config.update(SECRET_KEY=secrets.token_bytes(48), MAX_CONTENT_LENGTH=UPLOAD_LIMIT + 2 * 1024 ** 2,
                      MAX_FORM_MEMORY_SIZE=512 * 1024, MAX_FORM_PARTS=8,
                      SESSION_COOKIE_NAME="zvukbook_local", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict")
    action_lock = threading.RLock()

    @app.before_request
    def guard():
        local_port = app.config.get("LOCAL_PORT", port)
        if request.host not in (f"127.0.0.1:{local_port}", f"localhost:{local_port}"):
            return jsonify(error="Разрешён только локальный адрес."), 403
        internal_token = app.config.get("SUPERVISOR_TOKEN")
        if internal_token and not secrets.compare_digest(request.headers.get("X-Zvukbook-Token", ""), internal_token):
            return jsonify(error="Нужен локальный supervisor."), 403
        if request.method not in ("GET", "HEAD", "POST"):
            return jsonify(error="Метод не поддерживается."), 405
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="Недопустимый источник запроса."), 403
        if request.endpoint == "edit_metadata" and request.mimetype == "multipart/form-data":
            request.max_content_length = MAX_SOURCE_BYTES + 64 * 1024
        elif request.endpoint != "upload":
            request.max_content_length = 4 * 1024 ** 2
        if request.method == "POST":
            if request.headers.get("Origin") != "http://" + request.host:
                return jsonify(error="Открой локальную страницу и повтори."), 403
            token = request.headers.get("X-CSRF-Token", "")
            expected = session.get("csrf_token", "")
            if not token or not expected or not secrets.compare_digest(token, expected):
                return jsonify(error="Страница устарела. Обнови её перед сохранением."), 403

    @app.after_request
    def headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Cross-Origin-Resource-Policy": "same-origin", "Referrer-Policy": "same-origin",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.errorhandler(ValueError)
    @app.errorhandler(LookupError)
    @app.errorhandler(HTTPException)
    def error(exception):
        code = 409 if isinstance(exception, Conflict) else 404 if isinstance(exception, LookupError) else getattr(exception, "code", 400)
        message = "Файл/запрос слишком большой: книга до 100 MiB, обложка до 16 MiB, JSON до 4 MiB." if code == 413 else str(exception)
        return jsonify(error=message), code

    if WINDOWS:
        @app.errorhandler(Exception)
        def unexpected_error(exception):
            from .diagnostics import record_failure
            record_failure("backend_request_failed", exception, endpoint=request.endpoint, method=request.method)
            return jsonify(error=f"Внутренняя ошибка приложения ({type(exception).__name__}). "
                                 "Подробности в %LOCALAPPDATA%\\Zvukbook\\backend\\worker.log."), 500

    @app.get("/")
    def home():
        return render_template("index.html", device=limits.get("device", "cuda"), allow_doc=not WINDOWS, desktop=WINDOWS)

    def send_local_file(path, **options):
        if not WINDOWS:
            return send_file(path, **options)
        from .platform.windows_files import shared_read
        stream = shared_read(path)
        try:
            stat = os.fstat(stream.fileno())
            options.pop("conditional", None)
            response = send_file(stream, conditional=False, etag=f"{stat.st_mtime_ns}-{stat.st_size}",
                                 last_modified=stat.st_mtime, **options)
            response.content_length = stat.st_size
            response.make_conditional(request.environ, accept_ranges=True, complete_length=stat.st_size)
            response.call_on_close(stream.close)
            return response
        except Exception:
            stream.close()
            raise

    @app.get("/api/state")
    def state():
        desktop_settings = None
        if WINDOWS:
            desktop_settings = app.config.get('DESKTOP_SETTINGS')
            if desktop_settings is None:
                from .platform.windows import settings as host_settings
                desktop_settings = host_settings()
                app.config['DESKTOP_SETTINGS'] = desktop_settings
            limits.update(device=desktop_settings['device'], ram_max_bytes=desktop_settings['worker_memory_ceiling_bytes'])
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_hex(32)
        with database() as connection:
            books = [dict(row) for row in connection.execute("""SELECT b.*,
                (SELECT count(*) FROM chapter_audio a
                 WHERE a.book_id=b.id AND a.chapter>0 AND a.status='ready') AS audio_count
                FROM books b ORDER BY b.created_at DESC""")]
            jobs = [dict(row) for row in connection.execute("SELECT id,book_id,kind,status,error,progress,options FROM jobs ORDER BY created_at DESC LIMIT 30")]
            for book in books:
                try:
                    cover = visible_cover(book, connection)
                    book["cover_url"] = f"/api/books/{book['id']}/cover?v={cover.stat().st_mtime_ns}" if cover else "/static/logo.svg"
                except (OSError, ValueError):
                    book["cover_url"] = "/static/logo.svg"
        for job in jobs:
            job["progress"] = json.loads(job["progress"])
            options = json.loads(job.pop("options"))
            job["document_revision"] = options.get("document_revision", options.get("settings", {}).get("document_revision"))
            if WINDOWS:
                job["diagnostics_directory"] = str(job_dir(job["id"]))
        return jsonify(csrf_token=session["csrf_token"], books=books, jobs=jobs, speakers=SPEAKERS,
                       model=MODEL_ID, limits=limits, ram_available_bytes=psutil.virtual_memory().available,
                       disk_free_bytes=shutil.disk_usage(LIBRARY).free, desktop_settings=desktop_settings)

    @app.post('/api/desktop/device')
    def select_device():
        if not WINDOWS:
            raise LookupError('Настройка доступна в Windows-приложении.')
        payload = body()
        if payload.get('device') not in ('cpu', 'cuda'):
            raise ValueError('Выбери процессор или NVIDIA.')
        with action_lock, database() as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running')").fetchone():
                raise Conflict('Сначала дождись завершения или останови задачи в очереди.')
            from .platform.windows import settings as host_settings
            result = host_settings(payload['device'])
            app.config['DESKTOP_SETTINGS'] = result
            limits.update(device=result['device'], ram_max_bytes=result['worker_memory_ceiling_bytes'])
        return jsonify(result)

    @app.post("/api/books/upload")
    def upload():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            raise ValueError("Выбери файл книги.")
        filename = Path(uploaded.filename.replace("\\", "/")).name[:180]
        lower = filename.lower()
        kind = "fb2.zip" if lower.endswith((".fb2.zip", ".zip")) else Path(lower).suffix.lstrip(".")
        if WINDOWS and kind == "doc":
            raise ValueError("DOC пока не поддерживается в Windows. Сохрани книгу как DOCX и загрузи её.")
        if kind not in ("txt", "fb2", "fb2.zip", "epub", "docx", "doc", "pdf", "djvu", "djv"):
            raise ValueError("Поддерживаются TXT, FB2, FB2.zip, EPUB, DOCX, " + ("" if WINDOWS else "DOC, ") + "PDF и DJVU/DJV.")
        if shutil.disk_usage(LIBRARY).free < DISK_RESERVE + UPLOAD_LIMIT:
            raise Conflict("Недостаточно места: нужен резерв 5 GiB плюс размер книги.")
        book_id = uuid4().hex
        directory = book_dir(book_id)
        directory.mkdir(mode=0o700)
        source = directory / ("source." + kind)
        try:
            size = 0
            with source.open("xb") as target:
                while chunk := uploaded.stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > UPLOAD_LIMIT:
                        raise ValueError("Книга превышает 100 MiB.")
                    target.write(chunk)
            if not size:
                raise ValueError("Файл пуст.")
            sha = digest(source)
            with action_lock, database() as connection:
                existing = connection.execute("SELECT id FROM books WHERE source_sha256=?", (sha,)).fetchone()
                if existing:
                    shutil.rmtree(directory)
                    return jsonify(book_id=existing[0], duplicate=True)
                idle_book(connection, book_id, queue=True)
                connection.execute("INSERT INTO books(id,title,filename,format,source_sha256,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                                   (book_id, Path(filename).stem, filename, kind, sha, now()))
                job_id = new_job(connection, book_id, "import", {"options": import_options({})})
                if WINDOWS:
                    from .diagnostics import record
                    record("upload_stored", destination=job_dir(job_id) / "manager.log", source_bytes=size, format=kind)
            return jsonify(book_id=book_id, job_id=job_id), 202
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @app.get("/api/books/<book_id>")
    def book(book_id):
        result = fetch_book(book_id)
        try:
            document = read_book_document(result) if result["document_revision"] else None
        except BookDocumentUnavailable as error:
            document = None
            result.update(status="failed", error=str(error))
        with database() as connection:
            preview = connection.execute("SELECT duration,sha256 FROM chapter_audio WHERE book_id=? AND chapter=0",
                                         (book_id,)).fetchone()
        result["render_settings"] = json.loads(result["render_settings"])
        cover = visible_cover(result)
        result["cover"] = ({"url": f"/api/books/{book_id}/cover?v={cover.stat().st_mtime_ns}",
                            "custom": bool(result.get("custom_cover")), "default": False} if cover
                           else {"url": "/static/logo.svg", "custom": False, "default": True})
        result.update(chapter_list=chapter_records(book_id, document) if document else [],
                      preview=dict(preview) if preview and audio_path(book_id, 0).is_file() else None,
                      audio_directory=str(audio_dir(book_id)),
                      excluded=sorted(document.get("excluded", []), key=lambda entry: "chapter" not in entry)[:100] if document else [],
                      excluded_count=len(document.get("excluded", [])) if document else 0,
                      cleanup=document.get("cleanup", {}) if document else {},
                      import_options=document.get("import_options", {}) if document else {})
        result["pronunciation"] = validate_rules(document.get("pronunciation_rules", document.get("pronunciation")) if document else None)
        legacy = validate_rules(document.get("pronunciation") if document else None)
        result["legacy_pronunciation"] = bool(document and (legacy["replacements"] or legacy["stresses"])
            and any(chapter["index"] not in document.get("pronunciation_applied_chapters", []) for chapter in document["chapters"]))
        return jsonify(result)

    @app.get("/api/books/<book_id>/cover")
    def cover_image(book_id):
        path = visible_cover(fetch_book(book_id))
        if path is None:
            raise LookupError("Обложка пока не добавлена.")
        return send_local_file(path, mimetype="image/jpeg", conditional=True, download_name=COVER_NAME)

    @app.get("/api/books/<book_id>/cleanup-report")
    def cleanup_report(book_id):
        book = fetch_book(book_id)
        document = read_book_document(book)
        report = {"book_id": book_id, "document_revision": book["document_revision"],
                  "cleanup": document.get("cleanup", {}), "excluded": document.get("excluded", [])}
        response = jsonify(report)
        response.headers["Content-Disposition"] = 'attachment; filename="cleanup-report.json"'
        return response

    @app.post("/api/books/<book_id>/metadata")
    def edit_metadata(book_id):
        multipart = request.mimetype == "multipart/form-data"
        payload = request.form.to_dict() if multipart else body()
        uploaded = request.files.get("cover") if multipart else None
        if multipart and (uploaded is None or not uploaded.filename):
            raise ValueError("Выбери картинку обложки.")
        title, author = payload.get("title"), payload.get("author", "")
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 500
                or not isinstance(author, str) or len(author) > 500):
            raise ValueError("Укажи название до 500 знаков и автора до 500 знаков.")
        with action_lock:
            book = fetch_book(book_id)
            with database() as connection:
                idle_book(connection, book_id, queue=multipart)
            if book["status"] != "ready" or payload.get("document_revision") != book["document_revision"]:
                raise Conflict("Дождись готового текста и обнови книгу перед редактированием.")
            if multipart:
                if shutil.disk_usage(LIBRARY).free < DISK_RESERVE + MAX_SOURCE_BYTES + MAX_COVER_BYTES:
                    raise Conflict("Недостаточно места для новой обложки с сохранением резерва 5 GiB.")
                job_id = None
                try:
                    with database() as connection:
                        idle_book(connection, book_id, queue=True)
                        job_id = new_job(connection, book_id, "cover", {
                            "document_revision": book["document_revision"],
                            "title": " ".join(title.split()), "author": " ".join(author.split())})
                        size = 0
                        with (job_dir(job_id) / "cover-upload.image").open("xb") as target:
                            while chunk := uploaded.stream.read(1024 * 1024):
                                size += len(chunk)
                                if size > MAX_SOURCE_BYTES:
                                    raise ValueError("Обложка превышает 16 MiB.")
                                target.write(chunk)
                        if not size:
                            raise ValueError("Файл обложки пуст.")
                except Exception:
                    if job_id is not None:
                        shutil.rmtree(job_dir(job_id), ignore_errors=True)
                    raise
                return jsonify(book_id=book_id, job_id=job_id), 202
            document = read_book_document(book)
            document.update(title=" ".join(title.split()), author=" ".join(author.split()), metadata_edited=True)
            revision = save_document(book_id, document, expected_revision=book["document_revision"])
        return jsonify(document_revision=revision)

    @app.get("/api/books/<book_id>/chapters/<int:index>")
    def chapter(book_id, index):
        book = fetch_book(book_id)
        document = read_book_document(book)
        if not 1 <= index <= len(document["chapters"]):
            raise LookupError("Глава не найдена.")
        return jsonify(chapter=document["chapters"][index - 1], document_revision=book["document_revision"])

    @app.post("/api/books/<book_id>/pronunciation/preview")
    def pronunciation_preview(book_id):
        payload = body()
        book = fetch_book(book_id)
        if payload.get("document_revision") != book["document_revision"] or book["status"] != "ready":
            raise Conflict("Текст книги изменился. Закрой окно и открой его заново.")
        document = read_book_document(book)
        index = payload.get("chapter")
        if type(index) is not int or not 1 <= index <= len(document["chapters"]):
            raise ValueError("Выбери главу для предпросмотра.")
        text = payload.get("text", document["chapters"][index - 1]["text"])
        if not isinstance(text, str) or len(text) > 1_000_000:
            raise ValueError("Текст главы — до 1 млн знаков.")
        rules = validate_rules(payload.get("rules"))
        updated, changes = apply_to_text(text, rules)
        document["pronunciation"] = None
        document["pronunciation_materialized"] = True
        spoken = prepared_chapter(updated, document, profile_for(document))
        stats = {"replacements": [sum(change["kind"] == "replacement" for change in changes)],
                 "stresses": [sum(change["kind"] == "stress" for change in changes)]}
        return jsonify(spoken=spoken, stats=stats, rules=rules)

    @app.post("/api/books/<book_id>/pronunciation")
    def save_pronunciation(book_id):
        payload = body()
        rules = validate_rules(payload.get("rules"))
        with action_lock:
            book = fetch_book(book_id)
            with database() as connection:
                idle_book(connection, book_id)
            if book["status"] != "ready" or payload.get("document_revision") != book["document_revision"]:
                raise Conflict("Текст книги изменился. Закрой окно и открой его заново.")
            document = read_book_document(book)
            selected = payload.get("chapters")
            if (not isinstance(selected, list) or not selected or len(selected) > len(document["chapters"])
                    or any(type(index) is not int or not 1 <= index <= len(document["chapters"]) for index in selected)
                    or len(set(selected)) != len(selected)):
                raise ValueError("Выбери хотя бы одну главу для применения правил.")
            old_profile = profile_for(document)
            previous = {chapter["index"]: chapter_hash(chapter, document, old_profile) for chapter in document["chapters"]}
            edit = payload.get("chapter_edit")
            edited_text = None
            if edit is not None:
                if (not isinstance(edit, dict) or type(edit.get("index")) is not int
                        or not 1 <= edit["index"] <= len(document["chapters"])
                        or not isinstance(edit.get("text"), str) or not 1 <= len(edit["text"].strip()) <= 1_000_000):
                    raise ValueError("Укажи непустой текст главы, до 1 млн знаков.")
                edited_text = edit["text"].strip()
                document["chapters"][edit["index"] - 1]["text"] = edited_text
            changes = []
            for index in selected:
                chapter = document["chapters"][index - 1]
                updated, entries = apply_to_text(chapter["text"], rules, limit=20_000 - len(changes))
                if not updated.strip() or len(updated) > 1_000_000:
                    raise ValueError(f"Глава {index}: после применения текст пуст или больше 1 млн знаков.")
                chapter["text"] = updated
                for entry in entries:
                    entry.update(chapter=index, chapter_title=chapter["title"])
                changes.extend(entries)
            document["pronunciation_rules"] = rules
            document["pronunciation_materialized"] = True
            document["pronunciation_applied_chapters"] = sorted(set(document.get("pronunciation_applied_chapters", [])) | set(selected))
            profile = profile_for(document)
            affected = [chapter["index"] for chapter in document["chapters"]
                        if chapter_hash(chapter, document, profile) != previous[chapter["index"]]]
            revision = save_document(book_id, document, expected_revision=book["document_revision"])
        return jsonify(document_revision=revision, rules=rules, affected_chapters=affected,
                       changes=changes, edited_text=document["chapters"][edit["index"] - 1]["text"] if edit is not None else None)

    @app.post("/api/books/<book_id>/chapter")
    def edit_chapter(book_id):
        payload = body()
        with action_lock:
            book = fetch_book(book_id)
            with database() as connection:
                idle_book(connection, book_id)
            if payload.get("document_revision") != book["document_revision"]:
                raise Conflict("Текст изменился в другой вкладке. Обнови книгу.")
            index, title, text = payload.get("index"), payload.get("title"), payload.get("text")
            if (type(index) is not int or not 1 <= index <= book["chapters"] or not isinstance(title, str)
                    or not 1 <= len(title.strip()) <= 200 or not isinstance(text, str) or not 1 <= len(text.strip()) <= 1_000_000):
                raise ValueError("Недопустимый заголовок или текст главы.")
            document = read_book_document(book)
            document["chapters"][index - 1].update(title=title.strip(), text=text.strip())
            revision = save_document(book_id, document, expected_revision=book["document_revision"])
        return jsonify(document_revision=revision)

    @app.post("/api/books/<book_id>/import")
    def repeat_import(book_id):
        options = import_options(body())
        with action_lock, database() as connection:
            book = fetch_book(book_id)
            idle_book(connection, book_id, queue=True)
            source = book_dir(book_id) / ("source." + book["format"])
            if not source.is_file() or source.is_symlink():
                raise Conflict("Оригинал книги не найден. Восстанови его из резервной копии либо удали запись и загрузи книгу заново.")
            job_id = new_job(connection, book_id, "import", {"options": options})
        return jsonify(job_id=job_id), 202

    @app.post("/api/books/<book_id>/cleanup-notes")
    def cleanup_notes(book_id):
        payload = body()
        with action_lock:
            book = fetch_book(book_id)
            with database() as connection:
                idle_book(connection, book_id)
            if book["status"] != "ready" or payload.get("document_revision") != book["document_revision"]:
                raise Conflict("Дождись готового текста и обнови книгу перед очисткой.")
            document = read_book_document(book)
            summary = clean_document_notes(document)
            summary.update(clean_document_noise(document, force=True))
            document["normalizer_version"] = NORMALIZER_VERSION
            revision = save_document(book_id, document, expected_revision=book["document_revision"])
        return jsonify(document_revision=revision, **summary)

    @app.post("/api/books/<book_id>/render")
    def render_book(book_id):
        payload = body()
        if payload.get("chapter_storage") != 2:
            raise Conflict("Обновлено хранение MP3 по главам. Обнови страницу перед озвучкой.")
        with action_lock, database() as connection:
            desktop_settings = None
            if WINDOWS:
                from .platform.windows import settings as host_settings
                desktop_settings = host_settings()
                if not desktop_settings['configured']:
                    raise Conflict('Сначала выбери устройство озвучки в настройках приложения.')
            book = fetch_book(book_id)
            idle_book(connection, book_id, queue=True)
            if book["status"] != "ready" or payload.get("document_revision") != book["document_revision"]:
                raise Conflict("Дождись извлечения текста и обнови книгу перед рендером.")
            voice, rate, pitch = payload.get("voice", "xenia"), payload.get("rate", 1), payload.get("pitch", 0)
            if (not isinstance(voice, str) or voice not in SPEAKERS or type(rate) not in (int, float) or not math.isfinite(rate)
                    or not 0.75 <= rate <= 1.75 or type(pitch) is not int or pitch not in (-2, 0, 2)):
                raise ValueError("Неверные настройки голоса, скорости или высоты.")
            preview = payload.get("preview", False)
            if type(preview) is not bool:
                raise ValueError("Неверный режим рендера.")
            selected = [0] if preview else payload.get("chapters", list(range(1, book["chapters"] + 1)))
            if (not isinstance(selected, list) or not selected or len(selected) > 2000
                    or any(type(index) is not int or not (index == 0 and preview or 1 <= index <= book["chapters"]) for index in selected)):
                raise ValueError("Выбери хотя бы одну главу.")
            settings = {"voice": voice, "rate": round(rate, 2), "pitch": pitch,
                        "model": MODEL_ID, "model_sha256": MODEL_SHA256, "codec": "mp3-96k-mono-24k-v1",
                        "speech_normalizer_version": SPEECH_NORMALIZER_VERSION}
            if WINDOWS:
                settings['device'] = desktop_settings['device']
            for index in set(selected):
                connection.execute("""INSERT INTO chapter_audio(book_id,chapter,status,updated_at) VALUES(?,?,'pending',?)
                    ON CONFLICT(book_id,chapter) DO UPDATE SET status='pending',error=NULL,updated_at=excluded.updated_at""",
                    (book_id, index, now()))
            connection.execute("UPDATE books SET render_settings=? WHERE id=?", (json.dumps(
                {"voice": voice, "rate": settings["rate"], "pitch": pitch}, ensure_ascii=False), book_id))
            job_id = new_job(connection, book_id, "render", {"settings": settings, "document_revision": book["document_revision"],
                "preview": preview, "selected": sorted(set(selected))})
        return jsonify(job_id=job_id), 202

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel(job_id):
        with database() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (identity(job_id),)).fetchone()
            if row is None:
                raise LookupError("Задача не найдена.")
            connection.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job_id,))
            if row["status"] in ("queued", "publication_pending"):
                connection.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE id=?", (now(), job_id))
                if row["kind"] == "import":
                    connection.execute("UPDATE books SET status='cancelled',error='Обработка отменена.' WHERE id=?", (row["book_id"],))
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/publish")
    def retry_publication(job_id):
        body()
        with action_lock, database() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (identity(job_id),)).fetchone()
            if row is None:
                raise LookupError("Задача не найдена.")
            if row["kind"] != "render" or row["status"] != "publication_pending":
                raise Conflict("У этой задачи нет MP3, ожидающих публикации.")
            options = json.loads(row["options"])
            options["publication_only"] = True
            connection.execute("UPDATE jobs SET status='queued',cancel=0,error=NULL,options=? WHERE id=?",
                               (json.dumps(options, ensure_ascii=False), job_id))
        return jsonify(ok=True), 202

    def audio_record(book_id, index):
        fetch_book(book_id)
        with database() as connection:
            audio = connection.execute("SELECT * FROM chapter_audio WHERE book_id=? AND chapter=?", (book_id, index)).fetchone()
        if audio is None:
            raise LookupError("Глава ещё не готова.")
        path = audio_path(book_id, index)
        if path.is_symlink() or not path.is_file():
            raise LookupError("Аудиофайл отсутствует. Повтори рендер этой главы.")
        return path

    @app.get("/api/books/<book_id>/audio/<int:index>")
    def audio(book_id, index):
        path = audio_record(book_id, index)
        return send_local_file(path, mimetype="audio/mpeg", conditional=True, as_attachment=request.args.get("download") == "1",
                         download_name=f"{index:04d}.mp3")

    @app.get("/api/books/<book_id>/export")
    def export(book_id):
        with action_lock:
            return export_current(book_id)

    def export_current(book_id):
        book = fetch_book(book_id)
        with database() as connection:
            idle_book(connection, book_id)
        document = read_book_document(book)
        audios = [row for row in chapter_records(book_id, document) if row["ready"]]
        if not audios:
            raise LookupError("Для экспорта нужна хотя бы одна готовая глава.")
        titles = {chapter["index"]: chapter["title"] for chapter in document["chapters"]}
        files = [audio_record(book_id, row["index"]) for row in audios]
        cover = visible_cover(book)
        if any(digest(path) != row["sha256"] for row, path in zip(audios, files)):
            raise Conflict("Один из MP3 изменён вне приложения. Повтори рендер этой главы перед экспортом.")
        if shutil.disk_usage(LIBRARY).free < DISK_RESERVE + sum(path.stat().st_size for path in files) + (cover.stat().st_size if cover else 0):
            raise Conflict("Недостаточно места для ZIP. Можно скачать отдельные MP3.")
        manifest = {"schema": "projectpro-audiobook-v2", "title": document["title"], "author": document.get("author", ""),
                    "source_sha256": book["source_sha256"], "document_revision": book["document_revision"], "chapter_count": len(titles),
                    "complete": len(audios) == len(titles), "chapters": [dict(index=row["index"], title=row["title"],
                        file=f"{row['index']:04d}.mp3", duration_seconds=row["duration"], sha256=row["sha256"],
                        settings=row["settings"]) for row in audios]}
        if cover:
            manifest["cover"] = {"file": COVER_NAME, "mime_type": "image/jpeg", "sha256": digest(cover)}
        exports = LIBRARY / "exports"
        exports.mkdir(mode=0o700, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="audiobook-", suffix=".zip", dir=exports)
        os.close(descriptor)
        path = Path(name)
        try:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for audio, source in zip(audios, files):
                    archive.write(source, f"{audio['index']:04d}.mp3")
                if cover:
                    archive.write(cover, COVER_NAME)
            # Windows uses an explicit share-delete handle; the transfer owns it.
            return send_local_file(path, mimetype="application/zip", as_attachment=True, download_name=document["title"][:120] + ".zip")
        finally:
            path.unlink(missing_ok=True)

    @app.post("/api/books/<book_id>/delete")
    def delete(book_id):
        with action_lock, database() as connection:
            fetch_book(book_id)
            idle_book(connection, book_id)
            jobs = [row[0] for row in connection.execute("SELECT id FROM jobs WHERE book_id=?", (book_id,))]
            try:
                directory = audio_dir(book_id, connection)
                if directory != book_dir(book_id) / "mp3" and directory.exists():
                    shutil.rmtree(directory)
                directory = book_dir(book_id)
                if directory.exists():
                    shutil.rmtree(directory)
                for job_id in jobs:
                    directory = job_dir(job_id)
                    if directory.exists():
                        shutil.rmtree(directory)
            except OSError as error:
                raise Conflict("Не удалось удалить все файлы книги. Запись сохранена: проверь доступ к папке и повтори удаление.") from error
            connection.execute("DELETE FROM books WHERE id=?", (book_id,))
        return jsonify(ok=True)

    @app.post("/api/books/<book_id>/open-folder")
    def open_folder(book_id):
        body()
        with action_lock:
            fetch_book(book_id)
            directory = audio_dir(book_id)
            directory.mkdir(mode=0o700, exist_ok=True)
            if WINDOWS:
                os.startfile(str(directory))
                return jsonify(opened=True, path=str(directory))
            executable = shutil.which("gio") or shutil.which("xdg-open")
            if not executable:
                return jsonify(opened=False, path=str(directory))
            arguments = [executable, "open", str(directory)] if Path(executable).name == "gio" else [executable, str(directory)]
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                threading.Thread(target=process.wait, daemon=True).start()
                code = None
            if code not in (None, 0):
                raise Conflict("Файловый менеджер не открылся. Папка MP3: " + str(directory))
        return jsonify(opened=True, path=str(directory))

    return app
