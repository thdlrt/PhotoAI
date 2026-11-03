"""Per-photo durable analysis records, with read-only legacy recovery."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from .util import read_json, write_json


class _CheckpointReadCache:
    """Bounded positive cache; every lookup checks the current file identity."""

    def __init__(self, max_bytes: int = 8 * 1024 * 1024, max_entries: int = 4096):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries: OrderedDict[Path, tuple[tuple[int, ...], Any]] = OrderedDict()
        self.bytes = 0  # Serialized size, not an estimate of Python heap usage.
        self.lock = Lock()

    @staticmethod
    def _stamp(path: Path) -> tuple[int, ...]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino

    def _discard(self, path: Path) -> None:
        previous = self.entries.pop(path, None)
        if previous is not None:
            self.bytes -= previous[0][0]

    def read(self, path: Path) -> Any:
        path = path.absolute()
        try:
            for _ in range(3):
                stamp = self._stamp(path)
                with self.lock:
                    cached = self.entries.get(path)
                    if cached is not None and cached[0] == stamp:
                        self.entries.move_to_end(path)
                        return cached[1]
                    self._discard(path)
                value = read_json(path)
                if self._stamp(path) != stamp:
                    continue  # Replaced while being read: retry a fresh snapshot.
                with self.lock:
                    self._discard(path)
                    if stamp[0] <= self.max_bytes:
                        self.entries[path] = (stamp, value)
                        self.bytes += stamp[0]
                        while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
                            self._discard(next(iter(self.entries)))
                return value
            raise OSError("构图进度正在更新，请稍后重试。")
        except (OSError, ValueError):
            # Missing/corrupt/temporarily locked files must recover on the next
            # read even when the repaired file has exactly the same stat tuple.
            with self.lock:
                self._discard(path)
            raise


_read_cache = _CheckpointReadCache()


class DevelopCheckpoint:
    def __init__(self, run_dir: Path, context: dict[str, Any]) -> None:
        self.context = context
        digest = hashlib.sha256(
            json.dumps(context, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        self.directory = run_dir / "develop-checkpoints" / digest
        self.legacy_path = run_dir / "develop-checkpoint.json"
        self._legacy_items: dict[str, Any] | None = None

    def candidates(self, key: str) -> Iterator[dict[str, Any]]:
        try:
            record = _read_cache.read(self.directory / f"{key}.json")
            if (
                record.get("schema_version") == 2
                and record.get("context") == self.context
                and record.get("source_key") == key
                and isinstance(record.get("item"), dict)
            ):
                yield record["item"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        # Load the old aggregate only when a current record cannot be used.
        # Opening or resuming a legacy project never migrates/rewrites it.
        if self._legacy_items is None:
            self._legacy_items = {}
            try:
                legacy = _read_cache.read(self.legacy_path)
                if (
                    legacy.get("schema_version") == 1
                    and legacy.get("context") == self.context
                    and isinstance(legacy.get("items"), dict)
                ):
                    self._legacy_items = legacy["items"]
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        item = self._legacy_items.get(key)
        if isinstance(item, dict):
            yield item

    def save(self, key: str, item: dict[str, Any]) -> None:
        # Atomic replacement and Windows sharing-violation retries are shared
        # with other project state writes. No growing batch index is rewritten.
        write_json(self.directory / f"{key}.json", {
            "schema_version": 2,
            "context": self.context,
            "source_key": key,
            "item": item,
        })
