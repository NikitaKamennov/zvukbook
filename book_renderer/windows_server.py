"""Private Windows backend; started only by the desktop supervisor."""
import json
import msvcrt
import os
from pathlib import Path
import threading
import time

from waitress import create_server

from .config import MODEL_DIR, WINDOWS, WORKSPACE
from .manager import Manager
from .storage import initialize, write_json
from .web import create_app
from .diagnostics import configure_streams, record


def main():
    configure_streams()
    record("backend_started", pid=os.getpid())
    if not WINDOWS or not os.environ.get("ZVUKBOOK_SUPERVISOR_TOKEN"):
        raise RuntimeError("Windows backend запускается через Zvukbook.exe.")
    if not (MODEL_DIR / "model.pt").is_file():
        raise RuntimeError("В комплекте отсутствует Silero.")
    WORKSPACE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (WORKSPACE / "local-server.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        initialize()
        record("storage_initialized")
        limits = {"cpu_quota_vcpus": 0.25 * (os.cpu_count() or 1), "ram_max_bytes": 6 * 1024 ** 3,
                  "device": os.environ["ZVUKBOOK_DEVICE"], "isolation": "AppContainer"}
        app = create_app(0, limits)
        app.config["SUPERVISOR_TOKEN"] = os.environ["ZVUKBOOK_SUPERVISOR_TOKEN"]
        server = create_server(app, host="127.0.0.1", port=0, threads=4,
                               max_request_body_size=102 * 1024 ** 2, channel_timeout=60,
                               asyncore_loop_timeout=0.25)
        app.config["LOCAL_PORT"] = int(server.effective_port)
        manager = Manager()
        record("backend_ready", port=int(server.effective_port))
        stopping = threading.Event()

        def watch_close():
            while not stopping.wait(0.2):
                if (WORKSPACE / "backend-stop.request").exists():
                    manager.close()
                    server.asyncore.close_all(map=server._map)
                    return

        monitor = threading.Thread(target=watch_close, name="desktop-lifecycle", daemon=True)
        monitor.start()
        write_json(WORKSPACE / "backend-ready.json", {"pid": os.getpid(), "port": int(server.effective_port)})
        try:
            server.run()
        finally:
            record("backend_stopping")
            stopping.set()
            manager.close()
            server.close()
            server.task_dispatcher.shutdown()
            record("backend_stopped")


if __name__ == "__main__":
    main()
