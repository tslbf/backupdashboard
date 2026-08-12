"""An in-memory tail of the app's own log, so the UI can answer "is anything
actually happening right now?".

The Collectors page can show when a run started and what it recorded, but a run
that is *in progress* looks identical to a run that died three hours ago and
left its row behind. A live log is the difference between "it says running" and
"it is running".

Deliberately a bounded ring buffer in memory rather than a file or a table:
- it costs nothing when nobody is looking,
- it cannot grow without limit on a box that runs for months,
- and it is honest about being a tail. Anything that matters beyond the last few
  hundred lines belongs in the collector_runs rows, which are durable.

Entries carry a monotonic id so the UI can poll for "everything after N" instead
of re-fetching and re-rendering the whole buffer.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone

CAPACITY = 600

# Noisy per-request lines from the web server would drown out the collectors,
# which are the whole point of the panel.
MUTED_LOGGERS = ("uvicorn.access", "watchfiles")


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = CAPACITY):
        super().__init__()
        self._entries: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_id = 1

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(MUTED_LOGGERS):
            return
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{self.format(record)}"
        except Exception:  # noqa: BLE001 — logging must never raise into the app
            message = "<unformattable log record>"

        with self._lock:
            entry = {
                "id": self._next_id,
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
                .replace(tzinfo=None)
                .isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message[:4000],
            }
            self._next_id += 1
            self._entries.append(entry)

    def entries(self, after: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = [e for e in self._entries if e["id"] > after]
        return rows[-limit:]

    def last_id(self) -> int:
        with self._lock:
            return self._entries[-1]["id"] if self._entries else 0


_handler: RingBufferHandler | None = None


def install(level: int = logging.INFO) -> RingBufferHandler:
    """Attach to the root logger. Idempotent — uvicorn's reloader imports twice."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler()
        _handler.setLevel(level)
        _handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(_handler)
    return _handler


def get_handler() -> RingBufferHandler:
    return install()
