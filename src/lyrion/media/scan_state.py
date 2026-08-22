"""Shared library-scan progress state (rescan / rescanprogress).

A tiny module-level singleton both the CLI commands and the JSON-RPC
layer read. The importer updates it during a scan so controllers can
poll 'rescanprogress' like they do against the real LMS.
"""
from __future__ import annotations

import threading
from typing import Any


class ScanState:
    """Thread-safe scan progress holder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.scanning: bool = False
            self.progress: int = 0  # 0..100
            self.total_files: int = 0
            self.done_files: int = 0

    def start(self, total: int = 0) -> None:
        with self._lock:
            self.scanning = True
            self.progress = 0
            self.total_files = max(0, int(total))
            self.done_files = 0

    def update(self, done: int, total: int | None = None) -> None:
        with self._lock:
            if total is not None:
                self.total_files = max(0, int(total))
            self.done_files = max(0, int(done))
            if self.total_files > 0:
                self.progress = min(100, int(self.done_files * 100 / self.total_files))
            else:
                self.progress = 0

    def finish(self) -> None:
        with self._lock:
            self.scanning = False
            self.progress = 100 if self.done_files else 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "scanning": self.scanning,
                "progress": self.progress,
                "total_files": self.total_files,
                "done_files": self.done_files,
            }


#: Module-level singleton shared by all callers.
SCAN_STATE = ScanState()
