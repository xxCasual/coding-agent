from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RunLockHeld(RuntimeError):
    """Another live process holds the run file lock."""


@contextmanager
def run_file_lock(data_root: str | Path, run_id: str) -> Iterator[Path]:
    """Non-blocking flock for a single-machine worker. Released when the process exits."""
    root = Path(data_root).expanduser().resolve()
    lock_dir = root / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{run_id}.lock"
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunLockHeld(f"run lock held: {run_id}") from exc
        yield path
    finally:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
