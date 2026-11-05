#!/usr/bin/env python3
"""Local GPU audiobook preparation. Start with scripts/start-local.sh."""
import argparse
import fcntl
import os
import signal

from book_renderer.config import MODEL_DIR, WORKSPACE, require_scope_limits
from book_renderer.manager import Manager
from book_renderer.storage import initialize
from book_renderer.web import create_app


def stop_server(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Порт должен быть от 1024 до 65535.")
    limits = require_scope_limits()
    if not (MODEL_DIR / "model.pt").is_file():
        parser.error("Локальная модель не установлена; смотри docs/books-start.md.")
    os.umask(0o077)
    WORKSPACE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (WORKSPACE / "local-server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Локальная библиотека уже открыта другим сервером.")
        initialize()
        manager = Manager()
        signal.signal(signal.SIGTERM, stop_server)
        try:
            print(f"Книги: http://127.0.0.1:{args.port}/ · CUDA, одна задача, ресурсы ограничены.", flush=True)
            create_app(args.port, limits).run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False, debug=False)
        finally:
            manager.close()


if __name__ == "__main__":
    main()
