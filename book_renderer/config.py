from pathlib import Path
import os

WINDOWS = os.name == "nt"
ROOT = Path(os.path.abspath(__file__)).parents[1] if WINDOWS else Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / ".local-data/books-renderer"
LIBRARY = WORKSPACE / "library"
MODEL_DIR = WORKSPACE / "models/silero-v5_6_ru"
if WINDOWS:
    BUNDLE_ROOT = Path(os.environ.get("ZVUKBOOK_BUNDLE_ROOT", str(ROOT)))
    WORKSPACE = Path(os.environ.get("ZVUKBOOK_DATA_ROOT", str(Path(os.environ["LOCALAPPDATA"]) / "Zvukbook")))
    LIBRARY = WORKSPACE / "library"
    MODEL_DIR = BUNDLE_ROOT / "models/silero-v5_6_ru"
MODEL_SHA256 = "04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e"
MODEL_ID = "v5_6_ru"
SPEAKERS = {"xenia": "Ксения · xenia", "kseniya": "Ксения · kseniya", "baya": "Бая", "aidar": "Айдар", "eugene": "Евгений"}
SAMPLE_RATE = 24000
UPLOAD_LIMIT = 100 * 1024 ** 2
DISK_RESERVE = 5 * 1024 ** 3
RAM_RESERVE = 6 * 1024 ** 3
GPU_RESERVE = 1024 ** 3
GPU_FRACTION = 0.60
MAX_JOB_SECONDS = 6 * 60 * 60
NORMALIZER_VERSION = 5


def worker_limits():
    """Called inside a launched worker, never during package installation."""
    if WINDOWS:
        from .platform.windows_worker import require_worker_isolation
        require_worker_isolation()
        return
    import os
    import resource
    os.nice(10)
    if hasattr(os, "sched_getaffinity"):
        allowed = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, allowed[:4])
    # A per-file limit also bounds a failed converter's stdout/temporary output.
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 ** 3, 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def require_scope_limits():
    """Confirm kernel cgroup-v2 limits before starting the local server."""
    group = next((line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                  if line.startswith("0::")), None)
    if not group or ".." in group.split("/"):
        raise RuntimeError("Запускай через bash scripts/start-local.sh: нужны ограничения cgroup v2.")
    root = Path("/sys/fs/cgroup")
    current = root / group.lstrip("/")
    memory = []
    cpu = []
    pids = []
    while True:
        for filename, values in (("memory.max", memory), ("pids.max", pids)):
            path = current / filename
            if path.exists():
                value = path.read_text().strip()
                if value != "max":
                    values.append(int(value))
        path = current / "cpu.max"
        if path.exists():
            quota, period = path.read_text().split()
            if quota != "max":
                cpu.append(int(quota) / int(period))
        if current == root:
            break
        current = current.parent
    if not (memory and min(memory) <= 8 * 1024 ** 3 and cpu and min(cpu) <= 3 and pids and min(pids) <= 128):
        raise RuntimeError("Жёсткие ограничения CPU/RAM/PID не подтверждены. Используй scripts/start-local.sh.")
    return {"cpu_quota_vcpus": min(cpu), "ram_max_bytes": min(memory), "tasks_max": min(pids)}
