"""Single local queue, sandbox setup and publication of completed chapters."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

import psutil

from .platform import start_worker, stop_worker
from .config import DISK_RESERVE, LIBRARY, MAX_JOB_SECONDS, MODEL_DIR, RAM_RESERVE, ROOT, WORKSPACE, WINDOWS
from .storage import BookDocumentUnavailable, book_dir, database, digest, fetch_book, job_dir, now, read_book_document, read_json, write_json
from .audio import assign_audio_folder, audio_dir, audio_path, chapter_hash, prune_audio, sync_chapters
from .diagnostics import failure_details, record, record_failure
from .covers import publish_cover
from .pronunciation import profile_for, validate_rules


def job_event(job, event, **fields):
    record(event, destination=job_dir(job["id"]) / "manager.log", job_id=job["id"], kind=job["kind"], **fields)


def document_path(book_id, revision):
    if not isinstance(revision, str) or len(revision) != 64 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("Неверная версия текста.")
    return book_dir(book_id) / "canonical" / (revision + ".json")


def save_document(book_id, document, expected_revision=None):
    if "pronunciation" in document:
        document["pronunciation"] = validate_rules(document["pronunciation"])
    if isinstance(document.get("title"), str):
        document["title"] = " ".join(document["title"].split())
    chapters = document.get("chapters")
    if (document.get("schema") != "projectpro-local-book-v1" or not isinstance(chapters, list)
            or not 1 <= len(chapters) <= 2000):
        raise ValueError("Конвертер не вернул допустимую книгу.")
    total = 0
    for index, chapter in enumerate(chapters, 1):
        if (not isinstance(chapter, dict) or chapter.get("index") != index
                or not isinstance(chapter.get("text"), str) or not chapter["text"].strip()
                or not isinstance(chapter.get("title"), str) or len(chapter["title"]) > 200):
            raise ValueError("Некорректная глава после извлечения.")
        total += len(chapter["text"])
    if total > 20_000_000 or not isinstance(document.get("title"), str) or len(document["title"]) > 500:
        raise ValueError("Превышен допустимый объём книги.")
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")) > 60 * 1024 ** 2:
        raise ValueError("Подготовленный текст и отчёт очистки превышают 60 MiB.")
    revision = hashlib.sha256(encoded).hexdigest()
    target = document_path(book_id, revision)
    target.parent.mkdir(mode=0o700, exist_ok=True)
    if not target.exists():
        write_json(target, document)
    warnings = [str(value)[:1500] for value in document.get("warnings", [])][:100]
    with database() as connection:
        values = (document["title"], str(document.get("author", ""))[:500], len(chapters), revision,
                  json.dumps(warnings, ensure_ascii=False), book_id)
        sql = "UPDATE books SET title=?,author=?,status='ready',error=NULL,chapters=?,document_revision=?,warnings=? WHERE id=?"
        if expected_revision is not None:
            sql += " AND document_revision=?"
            values += (expected_revision,)
        if connection.execute(sql, values).rowcount != 1:
            raise ValueError("Текст уже изменился. Обнови книгу перед сохранением.")
        assign_audio_folder(connection, book_id, document)
        sync_chapters(connection, book_id, document)
    prune_audio(book_id)
    return revision


class Manager:
    def __init__(self):
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="books-local-queue", daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=15)

    def loop(self):
        while not self.stopping.is_set():
            job = None
            try:
                with database() as connection:
                    connection.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='queued' AND cancel=1", (now(),))
                    row = connection.execute("SELECT j.*,b.format FROM jobs j JOIN books b ON b.id=j.book_id WHERE j.status='queued' AND j.cancel=0 ORDER BY j.created_at LIMIT 1").fetchone()
                    job = dict(row) if row else None
                    if job:
                        connection.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (now(), job["id"]))
                if job:
                    self.execute(job)
            except Exception as error:
                record_failure("queue_failed", error, destination=job_dir(job["id"]) / "manager.log" if job else None)
                # A queue iteration failure must not create an automatic retry loop.
                if "job" in locals() and job:
                    retained = (job_dir(job["id"]) / "out/publication-pending.json").is_file()
                    self.finish(job, "publication_pending" if retained else "failed", "Ошибка очереди: " + str(error)[:700])
            self.stopping.wait(0.5)

    def finish(self, job, status, error=None):
        with database() as connection:
            connection.execute("UPDATE jobs SET status=?,error=?,finished_at=? WHERE id=?", (status, error, now(), job["id"]))
            if job["kind"] == "import" and status != "completed":
                connection.execute("UPDATE books SET status=?,error=? WHERE id=?", (status, error, job["book_id"]))
            if job["kind"] == "render":
                for index in json.loads(job["options"])["selected"]:
                    failed = index == job.get("active_chapter") and status in ("failed", "resource_paused")
                    connection.execute("""UPDATE chapter_audio SET status=?,error=? WHERE book_id=? AND chapter=?
                        AND status IN ('pending','rendering')""",
                        ("failed" if failed else "pending", error if failed else None, job["book_id"], index))
        job_event(job, "job_finished", status=status, error=error)

    def publish_audio(self, job, report):
        if job["kind"] != "render":
            return
        selected = set(json.loads(job["options"])["selected"])
        ready = report.get("ready", [])
        if not isinstance(ready, list) or len(ready) > 2001:
            raise ValueError("Неверный manifest фрагментов.")
        target = audio_dir(job["book_id"])
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        existing = job.setdefault("published", set())
        blocked = job.setdefault("publication_blocked", set())
        for audio in ready:
            index = audio.get("chapter")
            if type(index) is not int or index not in selected:
                raise ValueError("Неверный номер готовой главы.")
            if index in existing:
                continue
            expected = f"{index:04d}.mp3"
            source = job_dir(job["id"]) / "out" / expected
            duration = float(audio["duration"])
            # A manual publication retry can include chapters moved on an earlier pass.
            if not source.exists() and (target / expected).is_file():
                with database() as connection:
                    current = connection.execute("SELECT sha256,text_sha256 FROM chapter_audio WHERE book_id=? AND chapter=?",
                                                 (job["book_id"], index)).fetchone()
                if (current and current["sha256"] == audio.get("sha256")
                        and current["text_sha256"] == job["chapter_hashes"][index]
                        and digest(target / expected) == current["sha256"]):
                    existing.add(index)
                    blocked.discard(index)
                    continue
            if (audio.get("filename") != expected or source.is_symlink() or not source.is_file()
                    or not 0 < source.stat().st_size <= 1024 ** 3 or not math.isfinite(duration)
                    or not 0 < duration <= 24 * 60 * 60 or digest(source) != audio.get("sha256")):
                raise ValueError("Некорректный готовый MP3; файл не опубликован.")
            try:
                os.replace(source, target / expected)
            except PermissionError as error:
                if not WINDOWS:
                    raise
                blocked.add(index)
                record_failure("mp3_publication_blocked", error, destination=job_dir(job["id"]) / "manager.log", chapter=index)
                write_json(job_dir(job["id"]) / "out/publication-pending.json", {"retained": True})
                continue
            with database() as connection:
                connection.execute("""INSERT OR REPLACE INTO chapter_audio
                    (book_id,chapter,text_sha256,settings,duration,sha256,status,error,updated_at)
                    VALUES(?,?,?,?,?,?,'ready',NULL,?)""", (job["book_id"], index, job["chapter_hashes"][index],
                        json.dumps(json.loads(job["options"])["settings"], ensure_ascii=False),
                        duration, audio["sha256"], now()))
            existing.add(index)
            blocked.discard(index)
            job_event(job, "mp3_published", chapter=index)

    def read_worker_report(self, job, previous, *, terminal=False):
        names = ("result.json", "failure.json", "progress.json") if terminal else ("progress.json",)
        deferred = job.setdefault("deferred_reports", set())
        for name in names:
            path = job_dir(job["id"]) / "out" / name
            try:
                value = read_json(path, maximum=2 * 1024 ** 2)
            except FileNotFoundError:
                continue
            except OSError as error:
                if not WINDOWS or getattr(error, "winerror", None) not in (5, 32, 33):
                    raise
                if name not in deferred:
                    record_failure("worker_report_read_deferred", error, destination=job_dir(job["id"]) / "manager.log", report_file=name)
                    deferred.add(name)
                continue
            if not isinstance(value, dict):
                raise ValueError("Некорректный отчёт обработчика.")
            if name in deferred:
                job_event(job, "worker_report_read_recovered", report_file=name)
                deferred.discard(name)
            return value
        return previous

    def publish_receipts(self, job, *, terminal=False):
        """Publish verified chapters even when the progress report is locked."""
        if job["kind"] != "render":
            return
        paths = sorted((job_dir(job["id"]) / "out").glob("ready-[0-9][0-9][0-9][0-9].json"))
        if len(paths) > 2001:
            raise ValueError("Слишком много отчётов готовых глав.")
        deferred = job.setdefault("deferred_receipts", set())
        for path in paths:
            index = int(path.name[6:10])
            if index in job.get("published", set()):
                continue
            try:
                audio = read_json(path, maximum=8192)
            except OSError as error:
                if terminal or not WINDOWS or getattr(error, "winerror", None) not in (5, 32, 33):
                    raise
                if index not in deferred:
                    record_failure("chapter_receipt_read_deferred", error, destination=job_dir(job["id"]) / "manager.log", chapter=index)
                    deferred.add(index)
                continue
            if not isinstance(audio, dict) or audio.get("chapter") != index:
                raise ValueError("Некорректный отчёт готовой главы.")
            deferred.discard(index)
            self.publish_audio(job, {"ready": [audio]})

    def retry_publication(self, job, report):
        # Bounded file publication only: never launch the model again for a lock.
        for attempt in range(3):
            self.publish_audio(job, report)
            if not job.get("publication_blocked"):
                return
            if attempt < 2:
                self.stopping.wait(0.5)

    def finish_publication(self, job, report, status, failure):
        if job.get("publication_blocked"):
            status = "publication_pending"
            failure = "Новый MP3 сохранён, но Windows не даёт заменить прежний файл. Закрой внешний плеер и нажми «Повторить публикацию MP3». Повторной озвучки не будет."
            write_json(job_dir(job["id"]) / "out/publication-pending.json", {"retained": True})
        self.finish(job, status, failure)
        if status != "publication_pending":
            shutil.rmtree(job_dir(job["id"]) / "out", ignore_errors=True)
            if job["kind"] == "cover":
                for path in (job_dir(job["id"]) / "cover-upload.image", job_dir(job["id"]) / "input/source.image"):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as error:
                        record_failure("cover_upload_cleanup_failed", error, destination=job_dir(job["id"]) / "manager.log")

    def execute(self, job):
        job_event(job, "job_preparing")
        options = json.loads(job["options"])
        if not WINDOWS and not options.get("publication_only") and psutil.virtual_memory().available < RAM_RESERVE + 2 * 1024 ** 3:
            self.finish(job, "resource_paused", "Недостаточно свободной RAM: перед стартом нужно хотя бы 8 GiB.")
            return
        if not options.get("publication_only") and shutil.disk_usage(LIBRARY).free < DISK_RESERVE:
            self.finish(job, "resource_paused", "На диске осталось меньше 5 GiB.")
            return
        book = fetch_book(job["book_id"])
        config = dict(options)
        if job["kind"] == "import":
            source = book_dir(job["book_id"]) / ("source." + book["format"])
            config.update(format=book["format"], title_hint=book["title"])
            config["keep_custom_cover"] = bool(book.get("custom_cover"))
            if book["document_revision"]:
                try:
                    previous = read_book_document(book)
                except BookDocumentUnavailable:
                    previous = {}
                    config["metadata_override"] = {key: book.get(key, "") for key in ("title", "author")}
                if previous.get("metadata_edited"):
                    config["metadata_override"] = {key: previous.get(key, "") for key in ("title", "author")}
                if book.get("custom_cover") and previous.get("cover"):
                    config["cover_override"] = previous["cover"]
                if previous.get("pronunciation"):
                    config["pronunciation_override"] = previous["pronunciation"]
                config["pronunciation_state_override"] = {key: previous[key] for key in
                    ("pronunciation_rules", "pronunciation_applied_chapters", "pronunciation_materialized") if key in previous}
            with database() as connection:
                connection.execute("UPDATE books SET status='importing',error=NULL WHERE id=?", (book["id"],))
        elif job["kind"] == "cover":
            if options["document_revision"] != book["document_revision"]:
                raise ValueError("Книга изменилась. Обнови её и выбери обложку ещё раз.")
            source = job_dir(job["id"]) / "cover-upload.image"
            config.update(cover_only=True, format="image")
        else:
            source = document_path(book["id"], options["document_revision"])
            document = read_json(source)
            chapters = {chapter["index"]: chapter for chapter in document["chapters"]}
            chapters[0] = document["chapters"][0]
            profile = profile_for(document)
            job["chapter_hashes"] = {index: chapter_hash(chapters[index], document, profile) for index in options["selected"]}
            config["skip"] = []  # An explicitly selected chapter is always regenerated.
        directory = job_dir(job["id"])
        if options.get("publication_only"):
            report = self.read_worker_report(job, {}, terminal=True)
            self.retry_publication(job, report)
            self.publish_receipts(job, terminal=True)
            complete = report.get("status") == "completed" and set(options["selected"]) == job.get("published", set())
            self.finish_publication(job, report, "completed" if complete else "failed",
                                    None if complete else "Опубликованы сохранённые MP3. Неозвученные главы можно запустить отдельно.")
            return
        write_json(directory / "config.json", config)
        job_event(job, "job_inputs_prepared", source_bytes=source.stat().st_size)
        process = None
        report = {"status": "running", "phase": "Запуск обработчика", "percent": 0}
        failure = None
        status = "failed"
        started = time.monotonic()
        busy_samples = 0
        psutil.cpu_percent()
        try:
            with (directory / "worker.log").open("wb") as log:
                job_event(job, "worker_start_requested")
                with database() as connection:
                    connection.execute("UPDATE jobs SET progress=? WHERE id=?", (json.dumps(report, ensure_ascii=False), job["id"]))
                # A cover uses the existing CPU import sandbox/budget, not a
                # second queue, the backend image decoder, or a GPU worker.
                worker_job = dict(job, kind="import", format="image") if job["kind"] == "cover" else job
                process = start_worker(worker_job, directory / "config.json", source, log)
                job_event(job, "worker_started", pid=process.pid)
                while True:
                    report = self.read_worker_report(job, report)
                    self.publish_audio(job, report)
                    self.publish_receipts(job)
                    if job["kind"] == "render" and type(report.get("chapter")) is int:
                        job["active_chapter"] = report["chapter"]
                        with database() as connection:
                            connection.execute("""UPDATE chapter_audio SET status='rendering',error=NULL
                                WHERE book_id=? AND chapter=? AND status='pending'""",
                                (job["book_id"], report["chapter"]))
                    with database() as connection:
                        connection.execute("UPDATE jobs SET progress=? WHERE id=?", (json.dumps(report, ensure_ascii=False), job["id"]))
                    if process.poll() is not None:
                        break
                    with database() as connection:
                        cancel = connection.execute("SELECT cancel FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
                    if cancel or self.stopping.is_set():
                        status = "cancelled" if cancel else "interrupted"
                        failure = "Задача остановлена. Готовые главы сохранены."
                        break
                    if not WINDOWS and psutil.virtual_memory().available < RAM_RESERVE:
                        status, failure = "resource_paused", "Свободной RAM осталось меньше 6 GiB. Готовые главы сохранены."
                        break
                    if shutil.disk_usage(LIBRARY).free < DISK_RESERVE:
                        status, failure = "resource_paused", "Резерв диска меньше 5 GiB. Готовые главы сохранены."
                        break
                    busy_samples = busy_samples + 1 if psutil.cpu_percent() >= 90 else 0
                    if busy_samples >= 15 or time.monotonic() - started > MAX_JOB_SECONDS:
                        status, failure = "resource_paused", "Сервер занят или превышено время задачи. Рендер можно продолжить позже."
                        break
                    self.stopping.wait(1)
                if process.poll() is None:
                    self.stop_process(process)
                report = self.read_worker_report(job, report, terminal=True)
                self.publish_audio(job, report)
                self.publish_receipts(job, terminal=True)
                if job["kind"] == "render" and type(report.get("chapter")) is int:
                    job["active_chapter"] = report["chapter"]
                if WINDOWS:
                    if report.get("status") in ("failed", "resource_paused"):
                        try:
                            write_json(directory / "failure.json", report)
                        except Exception as archive_error:
                            record_failure("failure_report_archive_failed", archive_error, destination=directory / "manager.log")
                    report.update(worker_pid=process.pid, worker_exit_code=process.returncode)
                job_event(job, "worker_exited", exit_code=process.returncode, status=report.get("status"))
                if failure is None:
                    if process.returncode == 0 and report.get("status") == "completed":
                        if job["kind"] == "import":
                            report["phase"] = "Публикация текста книги"
                            job_event(job, "document_publication_started")
                            document = read_json(directory / "out/document.json")
                            save_document(book["id"], document)
                            try:
                                cover_published = publish_cover(book["id"], directory / "out", document.get("cover"))
                            except (MemoryError, InterruptedError):
                                raise
                            except Exception as error:
                                record_failure("cover_publication_failed", error, destination=directory / "manager.log")
                                reason = failure_details(error)["error"][:240]
                                warnings = document.get("warnings", [])[:99] + ["Текст готов, но обложку не удалось сохранить в папку MP3. Причина: " + reason]
                                with database() as connection:
                                    connection.execute("UPDATE books SET warnings=? WHERE id=?", (json.dumps(warnings, ensure_ascii=False), book["id"]))
                            else:
                                if cover_published:
                                    job_event(job, "cover_published")
                            job_event(job, "document_published")
                            report["phase"] = "Готово"
                        elif job["kind"] == "cover":
                            description = read_json(directory / "out/cover.json", maximum=8192)
                            current = fetch_book(book["id"])
                            if current["document_revision"] != options["document_revision"]:
                                raise ValueError("Книга изменилась. Обнови её и выбери обложку ещё раз.")
                            document = read_json(document_path(book["id"], current["document_revision"]))
                            document.update(title=options["title"], author=options["author"],
                                            metadata_edited=True, cover=dict(description, source="custom"))
                            cover_warnings = ("Встроенная обложка не найдена", "Обложку извлечь не удалось",
                                              "Текст готов, но обложку не удалось сохранить")
                            document["warnings"] = [warning for warning in document.get("warnings", [])
                                                    if not str(warning).startswith(cover_warnings)]
                            publish_cover(book["id"], directory / "out", description, custom=True)
                            save_document(book["id"], document, expected_revision=current["document_revision"])
                            job_event(job, "custom_cover_published")
                        elif job.get("publication_blocked"):
                            self.retry_publication(job, report)
                        if job["kind"] == "render" and not job.get("publication_blocked") and set(options["selected"]) != job.get("published", set()):
                            raise ValueError("Обработчик завершился без MP3 всех выбранных глав. Готовые главы сохранены.")
                        status = "completed"
                    else:
                        status = "resource_paused" if report.get("status") == "resource_paused" or getattr(process, 'resource_paused', False) else "failed"
                        failure = report.get("error") or getattr(process, "reason", "") or (
                            f"Обработчик завершился с кодом {process.returncode} без итогового отчёта. "
                            "Смотри worker.log и host-report.json этой задачи.")
        except Exception as error:
            record_failure("job_execution_failed", error, destination=directory / "manager.log")
            failure = str(error)[:1500]
            if WINDOWS:
                report.update(failed_phase=report.get("phase"), **failure_details(error))
            if isinstance(error, MemoryError):
                status = 'resource_paused'
        finally:
            if process is not None and process.poll() is None:
                self.stop_process(process)
            with database() as connection:
                connection.execute("UPDATE jobs SET progress=? WHERE id=?", (json.dumps(report, ensure_ascii=False), job["id"]))
            self.finish_publication(job, report, status, failure)

    @staticmethod
    def stop_process(process):
        stop_worker(process)
