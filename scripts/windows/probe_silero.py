"""Windows stage 0: fixed invented text; no library, server, OCR or network API."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import time
import wave

GIB = 1024 ** 3
MODEL_SHA = "04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e"
SAMPLE_RATE = 24000
VOICE = "xenia"
FRAGMENTS = (
    "Утром Анна открыла окно. Над вымышленным городом медленно плыли облака.",
    "На столе лежал синий блокнот. В нём она записала простую мысль: каждый опыт начинается с вопроса.",
    "Проверим спокойную речь, паузы и букву ё. Затем сохраним короткую запись и послушаем её целиком.",
)


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("processes", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class CPULimits(ctypes.Structure):
    _fields_ = [("flags", wintypes.DWORD), ("rate", wintypes.DWORD)]


class ProcessMemory(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "peak_ws", "ws", "peak_paged_pool", "paged_pool", "peak_nonpaged_pool",
            "nonpaged_pool", "pagefile", "peak_pagefile", "private")]


class PerformanceInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "commit_total", "commit_limit", "commit_peak", "physical_total", "physical_available",
            "cache", "kernel_total", "kernel_paged", "kernel_nonpaged", "page_size")
    ] + [(name, wintypes.DWORD) for name in ("handles", "processes", "threads")]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_report(output, report):
    partial = output / "worker-report.json.part"
    partial.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, output / "worker-report.json")


def verify_job(launcher):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.c_void_p)
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    member = wintypes.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(member)) or not member.value:
        raise RuntimeError("Start through the Go launcher: a Job Object is required before importing torch.")
    limits, cpu = ExtendedLimits(), CPULimits()
    for info_class, info in ((9, limits), (15, cpu)):
        if not kernel.QueryInformationJobObject(None, info_class, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
    # NULL queries the ambient job, which may be an outer job of an IDE/terminal.
    # Go queries our exact job handle and verifies membership while the child is suspended.
    required_flags = 0x8 | 0x200 | 0x2000
    if (launcher.get("job_verified_before_resume") is not True or launcher.get("job_limit_flags") != required_flags
            or not 2 * GIB <= launcher["job_commit_limit_bytes"] <= 6 * GIB
            or launcher.get("cpu_hard_cap_percent") != 25 or launcher.get("process_limit") != 8):
        raise RuntimeError("The supervisor did not attest the required limits before resuming this worker.")
    return {"commit_limit_bytes": launcher["job_commit_limit_bytes"], "cpu_rate": 2500,
            "active_process_limit": 8, "membership_verified_by": "supervisor_exact_job_handle",
            "ambient_job_flags": limits.basic.flags, "ambient_cpu_rate": cpu.rate}


def process_memory():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    info = ProcessMemory()
    info.size = ctypes.sizeof(info)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(info), info.size):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"peak_working_set_bytes": info.peak_ws, "private_commit_bytes": info.private}


class ProbeCancelled(RuntimeError):
    pass


def verify_container(args, report):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        member, needed = wintypes.DWORD(), wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 29, ctypes.byref(member), ctypes.sizeof(member), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        if member.value != 1:
            raise RuntimeError("Worker is not an AppContainer.")
        advapi.GetTokenInformation(token, 30, None, 0, ctypes.byref(needed))
        capabilities = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 30, capabilities, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        if wintypes.DWORD.from_buffer(capabilities).value != 0:
            raise RuntimeError("Unexpected AppContainer capabilities.")
    finally:
        kernel.CloseHandle(token)
    try:
        args.isolation_canary.read_bytes()
    except PermissionError:
        pass
    else:
        raise RuntimeError("AppContainer could read the private canary.")
    try:
        with (args.model.parent / "forbidden-write.txt").open("xb") as stream:
            stream.write(b"Unexpected model-directory write access")
    except PermissionError:
        pass
    else:
        raise RuntimeError("AppContainer could write into the model directory.")
    with socket.socket() as connection:
        connection.settimeout(1)
        result = connection.connect_ex(("127.0.0.1", args.network_canary_port))
    if result not in (10013, 10035, 10060):
        raise RuntimeError(f"AppContainer network canary had unexpected result {result}.")
    report["appcontainer"] = {"token_is_appcontainer": True, "capabilities": 0,
                              "private_canary_read_denied": True, "model_write_denied": True,
                              "loopback_connection_blocked": True, "loopback_error": result}
    report["isolation"] = "appcontainer-no-network"
    write_report(args.output, report)


def checkpoint(args):
    cancel = args.output / "cancel.request"
    if cancel.exists():
        raise ProbeCancelled(cancel.read_text(encoding="utf-8")[:300])
    if shutil.disk_usage(args.output).free < 5 * GIB + 256 * 1024 ** 2:
        raise MemoryError("Disk reserve exhausted.")
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetPerformanceInfo.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    psapi.GetPerformanceInfo.restype = wintypes.BOOL
    info = PerformanceInfo()
    info.size = ctypes.sizeof(info)
    if not psapi.GetPerformanceInfo(ctypes.byref(info), info.size):
        raise ctypes.WinError(ctypes.get_last_error())
    if not info.page_size or info.commit_total > info.commit_limit:
        raise RuntimeError("Invalid system memory counters.")
    if (info.physical_available * info.page_size < args.ram_reserve
            or (info.commit_limit - info.commit_total) * info.page_size < args.commit_reserve):
        raise MemoryError("RAM/commit reserve exhausted at fragment boundary.")


def render(args, report):
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("This probe requires Windows x64.")
    if sys.version_info[:3] != (3, 12, 10):
        raise RuntimeError("The probe runtime is pinned to CPython 3.12.10 x64.")
    report["confirmed_job"] = verify_job(args.launcher)  # Must happen before any ML import.
    if args.isolation_canary:
        verify_container(args, report)
    if report["confirmed_job"]["commit_limit_bytes"] != args.job_commit_limit:
        raise RuntimeError("Worker job memory limit differs from the launcher report.")
    report["python"] = sys.version
    report["windows"] = platform.platform()
    report["packages"] = {name: importlib.metadata.version(name) for name in (
        "torch", "numpy", "filelock", "typing_extensions", "setuptools", "sympy", "mpmath",
        "networkx", "Jinja2", "MarkupSafe", "fsspec")}
    expected = "2.11.0+" + ("cpu" if args.device == "cpu" else "cu128")
    if report["packages"]["torch"] != expected or report["packages"]["numpy"] != "2.2.6":
        raise RuntimeError("Runtime does not match the selected Windows lock.")
    lock_root = Path(os.path.abspath(__file__)).parents[2] / "requirements/windows-probe"
    report["runtime_locks_sha256"] = {name: digest(lock_root / name)
                                      for name in (args.device + ".lock", "common.lock")}
    checkpoint(args)
    if digest(args.model) != MODEL_SHA:
        raise ValueError("Silero SHA-256 mismatch; the model is never downloaded or replaced automatically.")
    report["model_sha256"] = MODEL_SHA
    report["ffmpeg_sha256"] = digest(args.ffmpeg)
    write_report(args.output, report)
    imported = time.monotonic()
    import torch
    from torch.package import PackageImporter
    report["torch_import_seconds"] = time.monotonic() - imported
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    device = torch.device("cpu")
    vram_reserve = None
    if args.device == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable. CPU fallback is disabled; choose the separate CPU probe explicitly.")
        device = torch.device("cuda:0")
        free, total = torch.cuda.mem_get_info(device)
        vram_reserve = max(GIB, total // 5)
        budget = min(total * 60 // 100, free - vram_reserve)
        if free < 2 * GIB or budget < GIB:
            raise MemoryError("Insufficient free VRAM after the system reserve.")
        torch.cuda.set_per_process_memory_fraction(budget / total, device)
        torch.cuda.reset_peak_memory_stats(device)
        report.update(gpu=torch.cuda.get_device_name(device), cuda_runtime=torch.version.cuda,
                      vram_free_before_bytes=free, vram_total_bytes=total,
                      allocator_budget_bytes=budget, vram_reserve_bytes=vram_reserve)
    checkpoint(args)
    loaded = time.monotonic()
    model = PackageImporter(str(args.model)).load_pickle("tts_models", "model")
    model.to(device)
    if args.device == "cuda":
        torch.cuda.synchronize(device)
    report["model_load_seconds"] = time.monotonic() - loaded
    if VOICE not in model.speakers:
        raise ValueError("The pinned voice is not present in the model.")
    report.update(phase="synthesis", fragments_done=0)
    write_report(args.output, report)
    synthesis = time.monotonic()
    wav_path = args.output / "sample.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for text in FRAGMENTS:
            checkpoint(args)
            if args.device == "cuda" and torch.cuda.mem_get_info(device)[0] < vram_reserve:
                raise MemoryError("VRAM reserve exhausted between fragments.")
            with torch.inference_mode():
                audio = model.apply_tts(text=text, speaker=VOICE, sample_rate=SAMPLE_RATE,
                                        put_accent=True, put_yo=True, put_stress_homo=True, put_yo_homo=True)
                audio = audio.detach().cpu()
            if (audio.ndim != 1 or not 0 < audio.numel() <= SAMPLE_RATE * 120
                    or not bool(torch.isfinite(audio).all())):
                raise ValueError("Model returned invalid or unexpectedly long audio.")
            pcm = audio.clamp(-1, 1).mul(32767).to(torch.int16).numpy().astype("<i2", copy=False)
            wav.writeframes(pcm.tobytes())
            wav.writeframes(b"\x00\x00" * int(SAMPLE_RATE * 0.12))
            report["fragments_done"] += 1
            if args.device == "cuda":
                report["allocator_peak_allocated_bytes"] = torch.cuda.max_memory_allocated(device)
                report["allocator_peak_reserved_bytes"] = torch.cuda.max_memory_reserved(device)
            write_report(args.output, report)
    report["synthesis_seconds"] = time.monotonic() - synthesis
    checkpoint(args)
    report["phase"] = "encoding_mp3"
    write_report(args.output, report)
    mp3_path = args.output / "sample.mp3"
    encoded = time.monotonic()
    subprocess.run([str(args.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-threads", "2", "-i", str(wav_path), "-filter_threads", "2", "-threads", "2",
                    "-c:a", "libmp3lame", "-b:a", "96k", "-ar", str(SAMPLE_RATE), "-ac", "1",
                    "-map_metadata", "-1", str(mp3_path)], check=True, timeout=60,
                   stdin=sys.stdin, stdout=sys.stdout, stderr=subprocess.PIPE,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    report["mp3_encode_seconds"] = time.monotonic() - encoded
    if not mp3_path.stat().st_size:
        raise ValueError("FFmpeg produced an empty MP3.")
    with wave.open(str(wav_path), "rb") as wav:
        report["audio_seconds"] = wav.getnframes() / wav.getframerate()
    report["synthesis_rtf"] = report["synthesis_seconds"] / report["audio_seconds"]
    report["wav_sha256"], report["mp3_sha256"] = digest(wav_path), digest(mp3_path)
    report.update(status="completed", phase="done")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--threads", type=int, choices=range(1, 9), required=True)
    parser.add_argument("--isolation-canary", type=Path)
    parser.add_argument("--network-canary-port", type=int, default=9)
    args = parser.parse_args()
    # Refuse alternate libraries/output roots even when invoked directly.
    # Supervisor resolves reparse points before launch. GetFinalPathNameByHandle
    # can require access to parents intentionally inaccessible to AppContainer.
    root = Path(os.path.abspath(__file__)).parents[2]
    runs = root / ".local-data/windows-x64/probe/runs"
    args.output = Path(os.path.abspath(args.output))
    if args.output.parent != runs or not (args.output / "launcher-report.json").is_file():
        raise RuntimeError("Use the Go launcher and its separate probe output directory.")
    launcher_path = args.output / "launcher-report.json"
    if launcher_path.stat().st_size > 64 * 1024:
        raise ValueError("Unexpected launcher report size.")
    launcher = json.loads(launcher_path.read_text(encoding="utf-8"))
    args.launcher = launcher
    if (launcher.get("schema") != "zvukbook-windows-job-probe-v1" or launcher.get("device") != args.device
            or launcher.get("status") != "running" or launcher.get("worker_pid") not in (os.getpid(), os.getppid())):
        raise RuntimeError("The launcher report does not describe this worker.")
    args.ram_reserve = int(launcher["ram_reserve_bytes"])
    args.commit_reserve = int(launcher["commit_reserve_bytes"])
    args.job_commit_limit = int(launcher["job_commit_limit_bytes"])
    if min(args.ram_reserve, args.commit_reserve, args.job_commit_limit) < 2 * GIB:
        raise ValueError("Invalid launcher memory budget/reserves.")
    trusted_model = root / ".local-data/books-renderer/models/silero-v5_6_ru/model.pt"
    private_model = root / ".local-data/windows-x64/probe/model/model.pt"
    if Path(os.path.abspath(args.model)) not in (trusted_model, private_model):
        raise RuntimeError("Only the existing, read-only Silero model path is allowed.")
    started = time.monotonic()
    report = {"schema": "zvukbook-windows-silero-probe-v1", "status": "running", "phase": "preflight",
              "worker_pid": os.getpid(), "parent_pid": os.getppid(),
              "device": args.device, "voice": VOICE, "sample_rate": SAMPLE_RATE,
              "model_id": "v5_6_ru", "threads": args.threads, "seed": 0,
              "tts_options": {"put_accent": True, "put_yo": True, "put_stress_homo": True, "put_yo_homo": True},
              "isolation": "job-object-only-synthetic-text",
              "text_origin": "fixed invented text in probe_silero.py"}
    try:
        render(args, report)
    except Exception as error:
        status = "cancelled" if isinstance(error, ProbeCancelled) else "failed"
        if isinstance(error, MemoryError) or "out of memory" in str(error).lower():
            status = "resource_paused"
        report.update(status=status, error_type=type(error).__name__, error=str(error)[:1500])
        if isinstance(error, subprocess.CalledProcessError):
            report["ffmpeg_error"] = (error.stderr or b"").decode("utf-8", errors="replace")[:1500]
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        try:
            report["process_memory"] = process_memory()
        except Exception as error:
            report["memory_query_error"] = str(error)
            if report["status"] == "completed":
                report.update(status="failed", error="Process memory measurement unavailable.")
        write_report(args.output, report)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
