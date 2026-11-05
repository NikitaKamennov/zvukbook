#!/usr/bin/env bash
set -euo pipefail
BOOKS_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BOOKS_PYTHON="$BOOKS_PROJECT_ROOT/.local-data/books-renderer/.venv/bin/python"
if [[ ! -x "$BOOKS_PYTHON" ]]; then
  echo 'Нет окружения книг. Смотри docs/books-start.md.' >&2
  exit 1
fi
cd -- "$BOOKS_PROJECT_ROOT"
exec systemd-run --user --scope --collect --unit=zvukbook-local \
  --property=CPUQuota=300% --property=CPUWeight=10 \
  --property=MemoryHigh=6G --property=MemoryMax=8G --property=MemorySwapMax=0 \
  --property=TasksMax=128 \
  "$BOOKS_PYTHON" -B zvukbook.py "$@"
