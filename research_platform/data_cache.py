from __future__ import annotations

import gc
import hashlib
import json
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pandas as pd
import psutil

from .config import PlatformConfig
from .storage import Database


DATA_CACHE_VERSION = "1"
FEATURE_CACHE_VERSION = "1"


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CacheMatch:
    cache_key: str
    snapshot_id: str
    hit_type: str
    data_asof: str
    query: dict[str, Any]
    coverage: dict[str, Any]


class MemoryLRU:
    def __init__(self, limit_bytes: int, minimum_available_bytes: int):
        self.limit_bytes = max(0, int(limit_bytes))
        self.minimum_available_bytes = max(0, int(minimum_available_bytes))
        self._items: OrderedDict[str, tuple[Any, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            if psutil.virtual_memory().available < self.minimum_available_bytes:
                self._items.clear()
                self._bytes = 0
                gc.collect()
                return None
            item = self._items.pop(key, None)
            if item is None:
                return None
            self._items[key] = item
            return item[0]

    def put(self, key: str, value: Any, size_bytes: int | None = None) -> bool:
        size = max(0, int(size_bytes if size_bytes is not None else estimate_size(value)))
        if self.limit_bytes <= 0 or size > self.limit_bytes:
            return False
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            self._evict_for(size)
            if (
                self._bytes + size > self.limit_bytes
                or psutil.virtual_memory().available < self.minimum_available_bytes
            ):
                if (
                    previous is not None
                    and psutil.virtual_memory().available >= self.minimum_available_bytes
                ):
                    self._items[key] = previous
                    self._bytes += previous[1]
                return False
            self._items[key] = (value, size)
            self._bytes += size
            return True

    def _evict_for(self, incoming: int) -> None:
        while self._items and (
            self._bytes + incoming > self.limit_bytes
            or psutil.virtual_memory().available < self.minimum_available_bytes
        ):
            _, (_, size) = self._items.popitem(last=False)
            self._bytes -= size

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def status(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._items),
                "size_bytes": self._bytes,
                "limit_bytes": self.limit_bytes,
                "minimum_available_bytes": self.minimum_available_bytes,
            }


def estimate_size(value: Any, seen: set[int] | None = None) -> int:
    seen = seen or set()
    object_id = id(value)
    if object_id in seen:
        return 0
    seen.add(object_id)
    if isinstance(value, pd.DataFrame):
        return int(value.memory_usage(index=True, deep=True).sum())
    if isinstance(value, pd.Series):
        return int(value.memory_usage(index=True, deep=True))
    if isinstance(value, dict):
        return sum(estimate_size(key, seen) + estimate_size(item, seen) for key, item in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return sum(estimate_size(item, seen) for item in value)
    try:
        import sys

        return int(sys.getsizeof(value))
    except TypeError:
        return 0


_MEMORY_CACHES: dict[str, MemoryLRU] = {}
_MEMORY_CACHES_LOCK = threading.Lock()


def shared_memory_cache(config: PlatformConfig) -> MemoryLRU:
    key = str(config.repository_root.resolve()).lower()
    with _MEMORY_CACHES_LOCK:
        cache = _MEMORY_CACHES.get(key)
        if cache is None:
            cache = MemoryLRU(
                config.performance.memory_cache_bytes,
                config.performance.minimum_available_memory_bytes,
            )
            _MEMORY_CACHES[key] = cache
        return cache


class DataCacheManager:
    def __init__(self, config: PlatformConfig, database: Database):
        self.config = config
        self.database = database
        self.memory = shared_memory_cache(config)
        self._flight_guard = threading.Lock()
        self._flight_locks: dict[str, threading.Lock] = {}

    def key(self, query: dict[str, Any]) -> str:
        return canonical_hash({"version": DATA_CACHE_VERSION, **query})

    def feature_key(
        self,
        snapshot_id: str,
        name: str,
        version: str,
        parameters: dict[str, Any] | None = None,
    ) -> str:
        return canonical_hash(
            {
                "version": FEATURE_CACHE_VERSION,
                "snapshot_id": snapshot_id,
                "feature": name,
                "feature_version": version,
                "parameters": parameters or {},
            }
        )

    def flight_lock(self, cache_key: str) -> threading.Lock:
        with self._flight_guard:
            return self._flight_locks.setdefault(cache_key, threading.Lock())

    def find(
        self,
        cache_key: str,
        *,
        identity: dict[str, Any],
        coverage: dict[str, Any],
    ) -> CacheMatch | None:
        rows = self.database.query(
            """SELECT * FROM data_cache_entries
            WHERE entry_type='SNAPSHOT' AND status='READY'
            ORDER BY CASE WHEN cache_key=? THEN 0 ELSE 1 END, last_accessed_at DESC""",
            (cache_key,),
        )
        for row in rows:
            query = _json_object(row.get("query_json"))
            cached_coverage = _json_object(row.get("coverage_json"))
            if str(row.get("cache_key")) != cache_key and not _coverage_matches(
                query.get("identity"), identity, cached_coverage, coverage
            ):
                continue
            snapshot_id = str(row.get("snapshot_id") or "")
            if not snapshot_id or not (self.config.snapshot_dir / snapshot_id).exists():
                continue
            self.touch(str(row["cache_key"]))
            return CacheMatch(
                cache_key=str(row["cache_key"]),
                snapshot_id=snapshot_id,
                hit_type="exact_hit" if str(row["cache_key"]) == cache_key else "superset_hit",
                data_asof=str(row.get("data_asof") or ""),
                query=query,
                coverage=cached_coverage,
            )
        return None

    def begin_snapshot(
        self,
        cache_key: str,
        snapshot_id: str,
        data_asof: str,
        query: dict[str, Any],
        coverage: dict[str, Any],
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        self.database.execute(
            """INSERT OR REPLACE INTO data_cache_entries
            (cache_key, entry_type, snapshot_id, data_asof, status, path, query_json,
             coverage_json, size_bytes, created_at, updated_at, last_accessed_at, error)
            VALUES (?, 'SNAPSHOT', ?, ?, 'BUILDING', ?, ?, ?, 0, ?, ?, ?, '')""",
            (
                cache_key,
                snapshot_id,
                data_asof,
                str(self.config.snapshot_dir / snapshot_id),
                json.dumps(query, ensure_ascii=False),
                json.dumps(coverage, ensure_ascii=False),
                now,
                now,
                now,
            ),
        )

    def ready_snapshot(self, cache_key: str, snapshot_id: str) -> None:
        now = datetime.now().astimezone().isoformat()
        size = directory_size(self.config.snapshot_dir / snapshot_id)
        self.database.execute(
            """UPDATE data_cache_entries SET status='READY', size_bytes=?, updated_at=?,
            last_accessed_at=?, error='' WHERE cache_key=?""",
            (size, now, now, cache_key),
        )
        # The caller records the backtest/run reference before applying disk governance.

    def commit_snapshot(
        self,
        build_key: str,
        cache_key: str,
        snapshot_id: str,
    ) -> None:
        """Atomically publish a completed snapshot without hiding an older READY entry."""
        now = datetime.now().astimezone().isoformat()
        size = directory_size(self.config.snapshot_dir / snapshot_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_cache_entries WHERE cache_key=? AND status='BUILDING'",
                (build_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Cache build is not publishable: {build_key}")
            connection.execute(
                """INSERT OR REPLACE INTO data_cache_entries
                (cache_key, entry_type, snapshot_id, data_asof, status, path, query_json,
                 coverage_json, size_bytes, created_at, updated_at, last_accessed_at, error)
                VALUES (?, 'SNAPSHOT', ?, ?, 'READY', ?, ?, ?, ?, ?, ?, ?, '')""",
                (
                    cache_key,
                    snapshot_id,
                    str(row["data_asof"] or ""),
                    str(row["path"] or ""),
                    str(row["query_json"] or "{}"),
                    str(row["coverage_json"] or "{}"),
                    size,
                    str(row["created_at"] or now),
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM data_cache_entries WHERE cache_key=?",
                (build_key,),
            )
        # Disk governance runs after the owning backtest records its snapshot reference.

    def fail(self, cache_key: str, error: str) -> None:
        now = datetime.now().astimezone().isoformat()
        rows = self.database.query(
            "SELECT path FROM data_cache_entries WHERE cache_key=?", (cache_key,)
        )
        size = directory_size(Path(str(rows[0].get("path") or ""))) if rows else 0
        self.database.execute(
            """UPDATE data_cache_entries SET status='FAILED', size_bytes=?, updated_at=?, error=?
            WHERE cache_key=?""",
            (size, now, error[:1000], cache_key),
        )

    def touch(self, cache_key: str) -> None:
        self.database.execute(
            "UPDATE data_cache_entries SET last_accessed_at=? WHERE cache_key=?",
            (datetime.now().astimezone().isoformat(), cache_key),
        )

    def get_or_build_feature_frames(
        self,
        cache_key: str,
        builder: Callable[[], pd.DataFrame | dict[str, pd.DataFrame]],
    ) -> tuple[pd.DataFrame | dict[str, pd.DataFrame], str]:
        memory_key = f"feature:{cache_key}"
        cached = self.memory.get(memory_key)
        if cached is not None:
            return cached, "memory_hit"
        with self.flight_lock(memory_key):
            cached = self.memory.get(memory_key)
            if cached is not None:
                return cached, "memory_hit"
            rows = self.database.query(
                "SELECT * FROM data_cache_entries WHERE cache_key=? AND status='READY'",
                (cache_key,),
            )
            if rows:
                path = Path(str(rows[0].get("path") or ""))
                try:
                    value = _load_feature(path)
                    self.memory.put(memory_key, value)
                    self.touch(cache_key)
                    return value, "disk_hit"
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            value = builder()
            target = self.config.cache_dir / "features" / cache_key
            temporary = target.with_name(f"{target.name}.tmp-{uuid4().hex}")
            temporary.mkdir(parents=True, exist_ok=False)
            try:
                _write_feature(temporary, value)
                if target.exists():
                    shutil.rmtree(target)
                temporary.replace(target)
            except Exception:
                if temporary.exists():
                    shutil.rmtree(temporary)
                raise
            now = datetime.now().astimezone().isoformat()
            self.database.execute(
                """INSERT OR REPLACE INTO data_cache_entries
                (cache_key, entry_type, snapshot_id, data_asof, status, path, query_json,
                 coverage_json, size_bytes, created_at, updated_at, last_accessed_at, error)
                VALUES (?, 'FEATURE', '', '', 'READY', ?, '{}', '{}', ?, ?, ?, ?, '')""",
                (
                    cache_key,
                    str(target),
                    directory_size(target),
                    now,
                    now,
                    now,
                ),
            )
            self.memory.put(memory_key, value)
            self.prune()
            return value, "built"

    def status(self) -> dict[str, Any]:
        rows = self.database.query(
            """SELECT entry_type, status, COUNT(*) AS entries,
            COALESCE(SUM(size_bytes), 0) AS size_bytes
            FROM data_cache_entries GROUP BY entry_type, status"""
        )
        protected = self._protected_snapshot_ids()
        protected_dependencies = self._protected_dependency_ids()
        protected_bytes = sum(
            directory_size(self.config.snapshot_dir / snapshot_id)
            for snapshot_id in protected
        )
        total = sum(int(row.get("size_bytes", 0) or 0) for row in rows)
        return {
            "entries": rows,
            "size_bytes": total,
            "limit_bytes": self.config.performance.disk_cache_bytes,
            "protected_snapshots": len(protected),
            "protected_dependencies": len(protected_dependencies),
            "protected_bytes": protected_bytes,
            "over_limit_protected": protected_bytes > self.config.performance.disk_cache_bytes,
            "memory": self.memory.status(),
        }

    def prune(self) -> dict[str, Any]:
        limit = self.config.performance.disk_cache_bytes
        rows = self.database.query(
            """SELECT * FROM data_cache_entries
            ORDER BY CASE
                WHEN status='FAILED' THEN 0
                WHEN entry_type='FEATURE' THEN 1
                ELSE 2
            END, last_accessed_at ASC"""
        )
        total = sum(int(row.get("size_bytes", 0) or 0) for row in rows)
        protected = self._protected_snapshot_ids()
        protected_dependencies = self._protected_dependency_ids()
        removed: list[str] = []
        for row in rows:
            failed = str(row.get("status") or "") == "FAILED"
            if total <= limit and not failed:
                break
            entry_type = str(row.get("entry_type") or "")
            snapshot_id = str(row.get("snapshot_id") or "")
            if entry_type == "SNAPSHOT" and snapshot_id in protected:
                continue
            if {
                str(row.get("cache_key") or ""),
                snapshot_id,
            } & protected_dependencies:
                continue
            path = Path(str(row.get("path") or ""))
            root = self.config.cache_dir if entry_type == "FEATURE" else self.config.snapshot_dir
            if path.exists() and _is_within(path, root):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            if entry_type == "SNAPSHOT" and snapshot_id:
                self.database.execute(
                    "DELETE FROM data_snapshots WHERE snapshot_id LIKE ?",
                    (f"{snapshot_id}:%",),
                )
            key = str(row["cache_key"])
            self.database.execute("DELETE FROM data_cache_entries WHERE cache_key=?", (key,))
            total -= int(row.get("size_bytes", 0) or 0)
            removed.append(key)
        return {"removed": removed, "size_bytes": max(0, total), "limit_bytes": limit}

    def _protected_snapshot_ids(self) -> set[str]:
        protected = {
            str(row["snapshot_id"])
            for row in self.database.query(
                "SELECT DISTINCT snapshot_id FROM backtests WHERE snapshot_id IS NOT NULL AND snapshot_id<>''"
            )
        }
        protected.update(
            str(row["snapshot_id"])
            for row in self.database.query(
                "SELECT DISTINCT snapshot_id FROM runs WHERE snapshot_id IS NOT NULL AND snapshot_id<>''"
            )
        )
        return protected

    def _protected_dependency_ids(self) -> set[str]:
        return {
            str(row["dependency_id"])
            for row in self.database.query(
                """SELECT DISTINCT dependency_id FROM snapshot_dependencies
                WHERE dependency_id<>''"""
            )
        }


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _coverage_matches(
    cached_identity: Any,
    requested_identity: dict[str, Any],
    cached: dict[str, Any],
    requested: dict[str, Any],
) -> bool:
    if cached_identity != requested_identity:
        return False
    cached_datasets = set(cached.get("datasets") or ())
    if not set(requested.get("datasets") or ()).issubset(cached_datasets):
        return False
    cached_start = str(cached.get("start_date") or "")
    cached_end = str(cached.get("end_date") or "")
    requested_start = str(requested.get("start_date") or "")
    requested_end = str(requested.get("end_date") or "")
    if not all((cached_start, cached_end, requested_start, requested_end)):
        return False
    if cached_start > requested_start or cached_end < requested_end:
        return False
    if int(cached.get("event_minimum_streak", 999) or 999) > int(
        requested.get("event_minimum_streak", 1) or 1
    ):
        return False
    return True


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_feature(
    directory: Path,
    value: pd.DataFrame | dict[str, pd.DataFrame],
) -> None:
    if isinstance(value, pd.DataFrame):
        value.to_parquet(directory / "frame.parquet")
        manifest = {"kind": "frame", "files": ["frame.parquet"]}
    else:
        files = []
        for name, frame in sorted(value.items()):
            filename = f"{name}.parquet"
            frame.to_parquet(directory / filename)
            files.append(filename)
        manifest = {"kind": "mapping", "files": files}
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_feature(directory: Path) -> pd.DataFrame | dict[str, pd.DataFrame]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") == "frame":
        return pd.read_parquet(directory / "frame.parquet")
    result: dict[str, pd.DataFrame] = {}
    for filename in manifest.get("files", []):
        path = directory / str(filename)
        result[path.stem] = pd.read_parquet(path)
    return result


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
