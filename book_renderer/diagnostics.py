"""Local Windows stage records; never log book text, command arguments or tokens."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from .config import WINDOWS


_started = time.monotonic()


def configure_streams():
    if WINDOWS:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)


def record(event, *, destination=None, **fields):
    if not WINDOWS:
        return
    try:
        entry = {"time": datetime.now(timezone.utc).isoformat(),
                 "elapsed_seconds": round(time.monotonic() - _started, 3), "event": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
        if destination is None:
            sys.stderr.write(line)
            sys.stderr.flush()
        else:
            with Path(destination).open("a", encoding="utf-8") as output:
                output.write(line)
    except Exception:
        pass  # Diagnostics must not replace the operation's original failure.


def failure_details(error):
    details = {"error_type": type(error).__name__,
               "error": str(error).strip()[:1500] or type(error).__name__ + ": исключение без сообщения"}
    for name in ("winerror", "errno"):
        if getattr(error, name, None) is not None:
            details["error_" + name] = getattr(error, name)
    if getattr(error, "json_replace_attempts", None) is not None:
        for name in ("json_target", "json_replace_attempts", "json_replace_wait_seconds"):
            details[name] = getattr(error, name)
        details["error"] = (f"Windows не позволила обновить {error.json_target} после "
                            f"{error.json_replace_attempts} попыток записи (WinError {error.winerror}). "
                            "Готовые главы сохранены. Подробности — в диагностике задачи.")
    if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        command = error.cmd
        name = Path(command[0]).name if isinstance(command, (list, tuple)) and command else "converter"
        details["tool"] = name
        if isinstance(error, subprocess.TimeoutExpired):
            details.update(tool_timeout_seconds=error.timeout, error=f"{name}: превышено время {error.timeout} с.")
        else:
            details.update(tool_exit_code=error.returncode, error=f"{name}: код завершения {error.returncode}.")
        # stdout may contain a book's text layer: do not add it to diagnostics.
        stderr = getattr(error, "stderr", None)
        if stderr:
            details["tool_stderr"] = (stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr))[-4000:]
    return details


def record_failure(event, error, *, destination=None, **fields):
    try:
        # Omit exception strings (CalledProcessError includes full command
        # arguments) and locals from the stack; details above supply the cause.
        frames = traceback.extract_tb(error.__traceback__)
        stack = [{"file": frame.filename, "line": frame.lineno, "function": frame.name} for frame in frames[-30:]]
        causes = []
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        seen = {id(error)}
        while cause is not None and id(cause) not in seen and len(causes) < 5:
            seen.add(id(cause))
            frames = traceback.extract_tb(cause.__traceback__)
            causes.append({**failure_details(cause), "stack": [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name} for frame in frames[-30:]]})
            cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
        record(event, destination=destination, **failure_details(error), **fields, stack=stack, causes=causes)
    except Exception:
        pass
