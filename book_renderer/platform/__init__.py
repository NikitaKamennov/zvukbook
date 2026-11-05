"""Platform boundaries; importing the Linux application never loads Win32 code."""
import os
from pathlib import Path
import subprocess
import sys
import time

from ..config import WINDOWS


def tool(name):
    if not WINDOWS:
        return "/usr/bin/" + name
    from ..config import BUNDLE_ROOT
    relative = {"ffmpeg": "ffmpeg/bin/ffmpeg.exe", "ffprobe": "ffmpeg/bin/ffprobe.exe",
                "tesseract": "tesseract/tesseract.exe", "ddjvu": "djvulibre/ddjvu.exe",
                "djvused": "djvulibre/djvused.exe"}
    path = BUNDLE_ROOT / "tools" / relative[name]
    if not path.is_file():
        raise ValueError("В сборке отсутствует инструмент: " + name + ". Пересобери комплект Windows.")
    return str(path)


def run_tool(arguments, **kwargs):
    if not WINDOWS:
        return subprocess.run(arguments, **kwargs)
    # AppContainer cannot open a fresh NUL device handle. These inherited
    # handles were explicitly supplied by Go and contain no user input.
    for name, inherited in (("stdin", sys.stdin), ("stdout", sys.stdout), ("stderr", sys.stderr)):
        if kwargs.get(name) == subprocess.DEVNULL:
            kwargs[name] = inherited
    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    from ..diagnostics import record, record_failure
    name = Path(arguments[0]).name
    started = time.monotonic()
    record("tool_started", tool=name, timeout_seconds=kwargs.get("timeout"))
    try:
        result = subprocess.run(arguments, **kwargs)
    except Exception as error:
        record_failure("tool_failed", error, tool_name=name, duration_seconds=round(time.monotonic() - started, 3))
        raise
    record("tool_finished", tool=name, exit_code=result.returncode, duration_seconds=round(time.monotonic() - started, 3))
    return result


def input_root():
    return Path(os.environ["ZVUKBOOK_WORKER_INPUT"]) if WINDOWS else Path("/input")


def output_root():
    return Path(os.environ["ZVUKBOOK_WORKER_OUTPUT"]) if WINDOWS else Path("/output")


def checkpoint():
    if WINDOWS:
        from .windows_worker import checkpoint as check
        check()


def start_worker(job, config, source, log):
    if WINDOWS:
        from .windows import start
    else:
        from .linux import start
    return start(job, config, source, log)


def stop_worker(process):
    if WINDOWS:
        process.cancel()
        process.wait(timeout=15)
    else:
        from .linux import stop
        stop(process)
