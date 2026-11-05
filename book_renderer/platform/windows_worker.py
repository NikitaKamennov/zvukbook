"""Fail-closed worker checks, using the supervisor's exact-job attestation."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil

_launch = None


class PerformanceInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in (
        'commit_total', 'commit_limit', 'commit_peak', 'physical_total', 'physical_available',
        'system_cache', 'kernel_total', 'kernel_paged', 'kernel_nonpaged', 'page_size')] + [
        (name, wintypes.DWORD) for name in ('handles', 'processes', 'threads')]


def require_worker_isolation():
    global _launch
    from . import input_root
    path = input_root() / "launch.json"
    if path.stat().st_size > 64 * 1024:
        raise ValueError("Некорректный отчёт supervisor.")
    launch = json.loads(path.read_text(encoding="utf-8"))
    if (launch.get("worker_pid") != os.getpid() or launch.get("job_verified_before_resume") is not True
            or launch.get("isolation") != "appcontainer-no-network" or launch.get("cpu_hard_cap_percent") != 25
            or launch.get('kind') not in ('import', 'render')
            or launch.get('device') not in ('cpu', 'cuda')
            or not (512 if launch.get('kind') == 'import' else 3072 if launch.get('device') == 'cuda' else 1024) * 1024 ** 2
            <= launch.get("job_commit_limit_bytes", 0) <= (6 if launch.get('device') == 'cuda' else 2) * 1024 ** 3):
        raise RuntimeError("Windows worker требует подтверждённые ограничения Go supervisor.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    kernel.IsProcessInJob.restype = wintypes.BOOL
    member = wintypes.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(member)) or not member.value:
        raise RuntimeError("Worker не находится в Job Object.")
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    token, needed, member = wintypes.HANDLE(), wintypes.DWORD(), wintypes.DWORD()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi.GetTokenInformation(token, 29, ctypes.byref(member), ctypes.sizeof(member), ctypes.byref(needed)) or member.value != 1:
            raise RuntimeError("Windows worker требует AppContainer.")
        advapi.GetTokenInformation(token, 30, None, 0, ctypes.byref(needed))
        capabilities = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 30, capabilities, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        if wintypes.DWORD.from_buffer(capabilities).value != 0:
            raise RuntimeError("У worker обнаружены лишние capabilities.")
    finally:
        kernel.CloseHandle(token)
    _launch = launch
    checkpoint()


def checkpoint():
    if _launch is None:
        raise RuntimeError("Worker не подтвердил изоляцию.")
    from . import output_root
    output = output_root()
    if (output / "cancel.request").exists():
        raise InterruptedError("Задача остановлена supervisor. Готовые главы сохранены.")
    if shutil.disk_usage(output).free < 5 * 1024 ** 3:
        raise MemoryError("Резерв диска меньше 5 GiB.")
    counters = PerformanceInfo()
    counters.size = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetPerformanceInfo.argtypes = (ctypes.POINTER(PerformanceInfo), wintypes.DWORD)
    if not psapi.GetPerformanceInfo(ctypes.byref(counters), counters.size):
        raise ctypes.WinError(ctypes.get_last_error())
    if (counters.physical_available * counters.page_size < _launch['ram_reserve_bytes']
            or (counters.commit_limit - counters.commit_total) * counters.page_size < _launch['commit_reserve_bytes']):
        raise MemoryError("Исчерпан резерв RAM/commit. Готовые главы сохранены.")
