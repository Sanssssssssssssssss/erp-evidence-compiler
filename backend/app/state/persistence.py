from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path


# ponytail: one process-wide lock matches Electron's single backend; use per-case/interprocess locks if deployment changes.
PERSISTENCE_LOCK = threading.RLock()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    with PERSISTENCE_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with PERSISTENCE_LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
