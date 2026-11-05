"""One sandboxed import/render operation. Started by the owner-run local app."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import wave

from .config import GPU_FRACTION, GPU_RESERVE, MODEL_DIR, MODEL_SHA256, SAMPLE_RATE, WINDOWS, worker_limits
from .platform import checkpoint, input_root, output_root, run_tool, tool
from .speech import TextSynthesisError, VERSION as SPEECH_VERSION, has_speech, prepare_text, synthesis_error_reason, synthesize_text
from .notes import clean_editor_notes
from .cleanup import clean_document_noise
from .storage import digest, read_json, write_json
from .diagnostics import configure_streams, failure_details, record, record_failure
from .pronunciation import profile_for


class ProgressWriter:
    """Progress is advisory; completed chapters have separate durable receipts."""
    def __init__(self, output):
        self.output = output
        self.retry_after = 0.0
        self.deferred = False
        self.failures = 0

    def save(self, report, *, force=False):
        # Retry on a later normal report update, never in a background loop.
        if not force and time.monotonic() < self.retry_after:
            return
        report["progress_write_deferred"] = False
        try:
            write_json(self.output / "progress.json", report)
        except OSError as error:
            if not WINDOWS or getattr(error, "winerror", None) not in (5, 32, 33):
                raise
            self.failures += 1
            self.deferred = True
            self.retry_after = time.monotonic() + 2
            report.update(progress_write_deferred=True, progress_write_failures=self.failures)
            record("progress_write_deferred", **failure_details(error))
        else:
            if self.deferred:
                record("progress_write_recovered", failed_updates=self.failures)
            self.deferred = False
            self.retry_after = 0.0


def pieces(text, maximum=500, *, prepared=False):
    from razdel import sentenize
    for paragraph in (text if prepared else prepare_text(text)).splitlines():
        paragraph = paragraph.strip()
        if not paragraph or not has_speech(paragraph):
            continue
        current = ""
        for sentence in sentenize(paragraph):
            value = sentence.text
            if len(current) + len(value) + 1 <= maximum:
                current = (current + " " + value).strip()
                continue
            if current:
                if has_speech(current):
                    yield current
                current = ""
            while len(value) > maximum:
                boundary = max(value.rfind(";", 0, maximum), value.rfind(",", 0, maximum))
                if boundary < maximum // 3:
                    boundary = value.rfind(" ", 0, maximum)
                if boundary < 1:
                    raise ValueError("Слишком длинное слово/формула. Исправь текст перед озвучкой.")
                part = value[:boundary + 1].strip()
                if has_speech(part):
                    yield part
                value = value[boundary + 1:].lstrip()
            current = value
        if current and has_speech(current):
            yield current


def render(config, output, report, progress_writer):
    report["phase"] = "Загрузка PyTorch"
    progress_writer.save(report)
    record("torch_loading")
    import torch
    from torch.package import PackageImporter
    record("torch_loaded", version=torch.__version__)
    model_path = MODEL_DIR / "model.pt" if WINDOWS else Path("/models/model.pt")
    report["phase"] = "Проверка модели"
    progress_writer.save(report)
    record("model_hash_started")
    if digest(model_path) != MODEL_SHA256:
        raise ValueError("Хеш модели не совпал с установленной версией. Перескачивание автоматически не выполняется.")
    record("model_hash_verified")
    device_name = os.environ.get("ZVUKBOOK_DEVICE", "cuda") if WINDOWS else "cuda"
    if device_name not in ("cpu", "cuda"):
        raise ValueError("Неверный профиль устройства.")
    if device_name == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA недоступна. Автоматическое переключение на CPU отключено.")
    vram_reserve = GPU_RESERVE
    if device_name == "cuda":
        free, total = torch.cuda.mem_get_info(0)
        if free < 2 * 1024 ** 3:
            raise MemoryError("Для старта нужно хотя бы 2 GiB свободной VRAM. Освободи видеопамять и повтори.")
        fraction = GPU_FRACTION
        if WINDOWS:
            vram_reserve = max(GPU_RESERVE, total // 5)
            budget = min(int(total * GPU_FRACTION), free - vram_reserve)
            if budget < 1024 ** 3:
                raise MemoryError("Недостаточно VRAM после системного резерва.")
            fraction = budget / total
        torch.cuda.set_per_process_memory_fraction(fraction, 0)
    cpu_threads = int(os.environ.get('ZVUKBOOK_CPU_THREADS', '2')) if WINDOWS else 2
    fragment_characters = int(os.environ.get('ZVUKBOOK_FRAGMENT_CHARACTERS', '500')) if WINDOWS else 500
    if not 1 <= cpu_threads <= 2 or not 100 <= fragment_characters <= 500:
        raise ValueError('Некорректный профиль потоков или размера фрагмента.')
    torch.set_num_threads(cpu_threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    report.update(phase="Загрузка голоса", device=device_name, gpu=torch.cuda.get_device_name(0) if device_name == "cuda" else None,
                  gpu_fraction=fraction if device_name == "cuda" else None)
    progress_writer.save(report)
    started = time.monotonic()
    record("model_loading", device=device_name, threads=cpu_threads)
    model = PackageImporter(str(model_path)).load_pickle("tts_models", "model")
    model.to(torch.device(device_name))
    report["model_load_seconds"] = time.monotonic() - started
    record("model_loaded", duration_seconds=round(report["model_load_seconds"], 3), device=device_name)
    settings = config["settings"]
    if settings["voice"] not in model.speakers:
        raise ValueError("У модели нет выбранного голоса.")
    document = read_json(input_root() / "source.json")
    noise_cleanup = clean_document_noise(document)
    record("narration_cleanup", **noise_cleanup)
    remove_notes = document.get("import_options", {}).get("remove_editor_notes", True)
    chapters = {chapter["index"]: chapter for chapter in document["chapters"]}
    if config.get("preview"):
        first = document["chapters"][0]
        narration, _, _ = clean_editor_notes(first["text"], remove_notes)
        short = " ".join(narration.split())
        if len(short) > 700:
            short = short[:700]
            boundary = short.rfind(" ")
            short = short[:boundary] if boundary > 0 else short
        chapters[0] = {"index": 0, "title": "Образец голоса", "text": short}
    selected = config["selected"]
    skipped = set(config.get("skip", []))
    total_chars = sum(len(chapters[index]["text"]) for index in selected if index not in skipped)
    completed_chars = 0
    report.update(text_sanitized_chapters=0, text_fallbacks=0, nonspoken_chapters=[], removed_editor_notes=0,
                  speech_normalizer_version=SPEECH_VERSION, **noise_cleanup)

    def generate(text):
        return model.apply_tts(text=text, speaker=settings["voice"], sample_rate=SAMPLE_RATE,
                               put_accent=True, put_yo=True, put_stress_homo=True, put_yo_homo=True)

    for index in selected:
        checkpoint()
        if index in skipped:
            continue
        chapter = chapters[index]
        profile = profile_for(document, 1 if index == 0 else index)
        record("chapter_started", chapter=index, characters=len(chapter["text"]))
        report.update(phase="Озвучка: " + chapter["title"], chapter=index, chapter_title=chapter["title"])
        wav_path = output / "current.wav"
        chapter_start = time.monotonic()
        synthesized_chars = 0
        fragments = 0
        narration, removed_notes, _ = clean_editor_notes(chapter["text"], remove_notes)
        report["removed_editor_notes"] += len(removed_notes)
        prepared = profile.prepare(narration, preserve_stress=(1 if index == 0 else index) in document.get("pronunciation_applied_chapters", []))
        if not has_speech(prepared) and any(character.isalpha() for character in profile.replace(narration)):
            raise TextSynthesisError(f"Глава {index}: после подготовки не осталось букв поддерживаемого русского алфавита. Проверь язык текста.")
        if prepared != chapter["text"]:
            report["text_sanitized_chapters"] += 1
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            for fragment_index, text in enumerate(pieces(prepared, maximum=fragment_characters, prepared=True), 1):
                checkpoint()
                report["fragment"] = fragment_index
                report["fragment_characters"] = len(text)
                if fragment_index == 1 or fragment_index % 25 == 0:
                    record("fragment_started", chapter=index, fragment=fragment_index)
                free = torch.cuda.mem_get_info(0)[0] if device_name == "cuda" else None
                if device_name == "cuda" and free < vram_reserve:
                    raise MemoryError("Осталось меньше 1 GiB VRAM. Рендер остановлен; готовые главы сохранены.")
                with torch.inference_mode():
                    try:
                        audio, used_fallback = synthesize_text(generate, text, prepared=True)
                    except (ValueError, AssertionError) as error:
                        raise TextSynthesisError(
                            f"Глава {index} «{chapter['title']}», фрагмент {fragment_index}: "
                            "модель не смогла обработать текст. Готовые главы сохранены. "
                            "Проверь этот участок текста перед повтором. Причина: " + synthesis_error_reason(error)[:500]) from error
                    report["text_fallbacks"] += int(used_fallback)
                    audio = audio.detach().cpu()
                if audio.ndim != 1 or not audio.numel() or not bool(torch.isfinite(audio).all()):
                    raise ValueError("Модель вернула некорректное аудио.")
                pcm = audio.clamp(-1, 1).mul(32767).to(torch.int16).numpy().astype("<i2", copy=False)
                wav.writeframes(pcm.tobytes())
                wav.writeframes(b"\x00\x00" * int(SAMPLE_RATE * 0.12))
                fragments += 1
                if WINDOWS and device_name == 'cpu':
                    time.sleep(0.08)
                synthesized_chars += len(text)
                report.update(percent=min(98, round((completed_chars + synthesized_chars) / max(total_chars, 1) * 100)),
                              gpu_peak_reserved_bytes=torch.cuda.max_memory_reserved(0) if device_name == "cuda" else None)
                progress_writer.save(report)
            if not fragments:
                # A section containing only *** / punctuation is a short pause,
                # not a malformed TTS call or an unreported gap in chapter order.
                wav.writeframes(b"\x00\x00" * int(SAMPLE_RATE * 0.4))
                report["nonspoken_chapters"].append(index)
        ratio = 2 ** (settings["pitch"] / 12)
        shifted_rate = round(SAMPLE_RATE * ratio)
        tempo = settings["rate"] / (shifted_rate / SAMPLE_RATE)
        filters = "asetrate=" + str(shifted_rate) + ",aresample=" + str(SAMPLE_RATE) + ",atempo=" + str(tempo)
        partial = output / (f"{index:04d}.mp3.part")
        target = output / (f"{index:04d}.mp3")
        report["phase"] = "Сборка MP3: " + chapter["title"]
        record("mp3_encoding", chapter=index, fragments=fragments)
        checkpoint()
        progress_writer.save(report)
        run_tool([tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-threads", str(cpu_threads), "-i", str(wav_path), "-filter_threads", str(cpu_threads), "-af", filters,
                        "-c:a", "libmp3lame", "-b:a", "96k", "-ar", str(SAMPLE_RATE), "-ac", "1",
                        "-map_metadata", "-1", "-metadata", "title=" + chapter["title"],
                        "-metadata", "track=" + str(index),
                        "-metadata", "album=" + document["title"], "-f", "mp3", str(partial)],
                       check=True, timeout=600, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        measured = run_tool([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                                   "-of", "json", str(partial)], check=True, timeout=30,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        duration = float(json.loads(measured.stdout)["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Не удалось получить длительность MP3.")
        sha = digest(partial)
        os.replace(partial, target)
        wav_path.unlink()
        elapsed = time.monotonic() - chapter_start
        audio_record = {"chapter": index, "filename": target.name, "duration": duration,
                        "sha256": sha, "render_seconds": elapsed, "rtf": elapsed / duration}
        # The independent failure report must also retain a completed MP3 if
        # persisting its receipt encounters a genuine output-directory error.
        report["ready"].append(audio_record)
        # One new receipt per chapter. It never replaces progress.json or an
        # earlier chapter's receipt; the manager can publish it independently.
        write_json(output / f"ready-{index:04d}.json", audio_record)
        completed_chars += len(chapter["text"])
        progress_writer.save(report)
        record("chapter_finished", chapter=index, duration_seconds=duration, render_seconds=round(elapsed, 3))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("import", "render"))
    args = parser.parse_args()
    configure_streams()
    output = None
    progress_writer = None
    report = {"status": "running", "phase": "Подготовка", "percent": 0, "ready": []}
    try:
        record("worker_started", action=args.action, pid=os.getpid())
        worker_limits()
        record("isolation_verified")
        output = output_root()
        progress_writer = ProgressWriter(output)
        config = read_json(input_root() / "job.json", maximum=1024 * 1024)
        record("job_config_loaded", action=args.action, format=config.get("format"))
        progress_writer.save(report)
        if args.action == "import" and config.get("cover_only"):
            from .covers import image_bytes, MAX_SOURCE_BYTES
            report["phase"] = "Подготовка новой обложки"
            progress_writer.save(report)
            checkpoint()
            source = input_root() / "source.image"
            if not 0 < source.stat().st_size <= MAX_SOURCE_BYTES:
                raise ValueError("Картинка обложки должна быть непустой и не больше 16 MiB.")
            description = image_bytes(source.read_bytes(), output)
            checkpoint()
            write_json(output / "cover.json", description)
            record("custom_cover_prepared", width=description["width"], height=description["height"])
        elif args.action == "import":
            from .extract import extract

            def progress(phase, current, total):
                checkpoint()
                report.update(phase=phase, current=current, total=total, percent=round(current / max(total, 1) * 95))
                record("import_progress", phase=phase, current=current, total=total, percent=report["percent"])
                progress_writer.save(report)

            source = input_root() / ("source." + config["format"])
            report["phase"] = "Чтение " + config["format"].upper()
            record("extraction_started", format=config["format"], source_bytes=source.stat().st_size)
            document = extract(source, config["format"], config["options"], output, progress,
                               include_cover=not config.get("keep_custom_cover", False))
            record("extraction_finished", chapters=len(document["chapters"]),
                   characters=sum(len(chapter["text"]) for chapter in document["chapters"]))
            if document.get("title") in ("", "source", "Книга", "Начало книги"):
                document["title"] = config["title_hint"]
            if config.get("metadata_override"):
                document.update(config["metadata_override"], metadata_edited=True)
            if config.get("cover_override"):
                document["cover"] = config["cover_override"]
            if config.get("pronunciation_override"):
                document["pronunciation"] = config["pronunciation_override"]
            document.update(config.get("pronunciation_state_override", {}))
            report["phase"] = "Сохранение текста книги"
            progress_writer.save(report)
            record("document_write_started")
            write_json(output / "document.json", document)
            record("document_write_finished")
        else:
            render(config, output, report, progress_writer)
        report.update(status="completed", phase="Готово", percent=100)
        # A distinct terminal report lets the host finish even when a foreign
        # handle keeps the advisory progress.json locked for the whole task.
        write_json(output / "result.json", report)
        progress_writer.save(report, force=True)
        record("worker_completed", action=args.action)
        return 0
    except Exception as error:
        failed_phase = report["phase"]
        record_failure("worker_failed", error, action=args.action, phase=failed_phase,
                       chapter=report.get("chapter"), fragment=report.get("fragment"))
        if not WINDOWS and isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
            for name in ("stdout", "stderr"):
                detail = getattr(error, name, None)
                if detail:
                    print(name + ": " + (detail.decode("utf-8", errors="replace") if isinstance(detail, bytes) else str(detail))[:8000], file=sys.stderr)
        resource_failure = isinstance(error, MemoryError) or "out of memory" in str(error).lower()
        report.update(status="resource_paused" if resource_failure else "failed", phase="Остановлено",
                      failed_phase=failed_phase, **failure_details(error))
        if output is not None:
            # A progress write may itself be the failure. Preserve its cause in
            # an independent new file before attempting that same path again.
            for name in ("failure.json", "progress.json"):
                try:
                    if name == "progress.json" and progress_writer is not None:
                        progress_writer.save(report, force=True)
                    else:
                        write_json(output / name, report)
                except Exception as report_error:
                    record_failure("failure_report_write_failed", report_error, report_file=name)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
