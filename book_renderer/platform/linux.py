"""Original Linux cgroup/bubblewrap worker host; independent of Windows desktop."""
import os
from pathlib import Path
import signal
import subprocess
from ..config import MODEL_DIR, ROOT, WORKSPACE
from ..storage import job_dir


def sandbox_command(job, config_path, source):
    output = job_dir(job["id"]) / "out"
    arguments = ["/usr/bin/bwrap", "--die-with-parent", "--unshare-net", "--unshare-pid", "--unshare-ipc", "--cap-drop", "ALL",
                 "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
                 "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                 "--ro-bind", "/sys", "/sys", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                 "--dir", "/tmp/home", "--dir", "/var/tmp", "--dir", "/input", "--dir", "/output",
                 "--ro-bind", str(WORKSPACE / ".venv"), "/venv",
                 "--ro-bind", str(ROOT / "book_renderer"), "/app/book_renderer",
                 "--ro-bind", str(config_path), "/input/job.json", "--bind", str(output), "/output",
                 "--chdir", "/app", "--setenv", "HOME", "/tmp/home", "--setenv", "XDG_CACHE_HOME", "/tmp/home/.cache"]
    for path in ("/etc/ld.so.cache", "/etc/alternatives", "/etc/fonts"):
        if Path(path).exists():
            arguments.extend(["--ro-bind", path, path])
    if job["kind"] == "render":
        arguments.extend(["--ro-bind", str(MODEL_DIR), "/models", "--ro-bind", str(source), "/input/source.json"])
        for path in ("/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools"):
            if Path(path).exists():
                arguments.extend(["--dev-bind", path, path])
    else:
        arguments.extend(["--ro-bind", str(source), "/input/source." + job["format"]])
    arguments.extend(["/venv/bin/python", "-B", "-m", "book_renderer.worker", job["kind"]])
    return arguments



def start(job, config, source, log):
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONNOUSERSITE": "1", "OMP_NUM_THREADS": "2", "OMP_THREAD_LIMIT": "2",
                   "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "NUMEXPR_NUM_THREADS": "2",
                   "CUDA_VISIBLE_DEVICES": "0", "CUDA_CACHE_PATH": "/tmp/home/cuda-cache"}
    return subprocess.Popen(sandbox_command(job, config, source), stdin=subprocess.DEVNULL, stdout=log,
                            stderr=log, env=environment, start_new_session=True)


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait(timeout=5)
