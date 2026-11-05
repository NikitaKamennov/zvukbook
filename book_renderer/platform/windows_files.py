"""Shared Windows reads and bounded atomic JSON replacement."""
import ctypes
from ctypes import wintypes
import msvcrt
import os
import time

from ..diagnostics import record, record_failure


def replace_json(source, target):
    """Retry only the rename of already-written JSON, never the job/model.

    Even share-delete readers cannot prevent other Windows processes from
    temporarily blocking a rename. Keep the old file intact and cap the wait.
    """
    delays = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4)
    waited = 0.0
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, target)
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33):
                raise
            if attempt == len(delays):
                error.json_target = target.name
                error.json_replace_attempts = attempt + 1
                error.json_replace_wait_seconds = round(waited, 3)
                record_failure("json_replace_failed", error)
                raise
            delay = delays[attempt]
            record("json_replace_retry", json_target=target.name, failed_attempt=attempt + 1,
                   winerror=error.winerror, next_delay_seconds=delay)
            time.sleep(delay)
            waited += delay
        else:
            if attempt:
                record("json_replace_recovered", json_target=target.name, attempts=attempt + 1,
                       wait_seconds=round(waited, 3))
            return


def shared_read(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")
