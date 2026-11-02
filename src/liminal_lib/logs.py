"""The log every program of Liminal writes: logs/liminal.log beside the database, rotated."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

LOG_MAX_BYTES = 20 * 1024 * 1024  # a log file is rotated past this ...
LOG_KEEP = 5  # ... and this many old ones are kept (liminal.log.1 newest): ~120 MB at most


class Logger:
    """Print and append to a log file next to the database, rotated at LOG_MAX_BYTES."""

    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        print(msg, flush=True)
        if self.path:
            self._rotate()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size < LOG_MAX_BYTES:
                return
            for i in range(LOG_KEEP - 1, 0, -1):
                older = self.path.with_name(f"{self.path.name}.{i}")
                if older.exists():
                    older.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass  # another process rotated it, or it is not there yet: write on


def log_to(db_path: Path | str) -> Logger:
    """The log of the database at `db_path` (logs/liminal.log beside it)."""
    return Logger(Path(db_path).parent / "logs" / "liminal.log")
