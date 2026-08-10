from __future__ import annotations

import inspect
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4


class JobManager:
    def __init__(self, max_workers: int = 3):
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)), thread_name_prefix="research-job"
        )
        self._jobs: dict[str, dict[str, Any]] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._active_fingerprints: dict[str, str] = {}
        self._lock = threading.Lock()

    def submit(self, job_type: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        fingerprint = _job_fingerprint(job_type, function, args, kwargs)
        job_id = uuid4().hex
        with self._lock:
            existing = self._active_fingerprints.get(fingerprint)
            if existing and self._jobs.get(existing, {}).get("status") in {"QUEUED", "RUNNING"}:
                return existing
            self._jobs[job_id] = {
                "job_id": job_id,
                "job_type": job_type,
                "status": "QUEUED",
                "created_at": datetime.now().astimezone().isoformat(),
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": "",
                "phase": "QUEUED",
                "progress": 0.0,
                "detail": "等待本地工作线程",
                "cache_status": "",
                "waiting_reason": "waiting_for_worker",
                "fingerprint": fingerprint,
            }
            self._active_fingerprints[fingerprint] = job_id
        future = self._executor.submit(self._run, job_id, function, args, kwargs)
        self._futures[job_id] = future
        return job_id

    def _run(
        self,
        job_id: str,
        function: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self._update(
            job_id,
            status="RUNNING",
            phase="STARTING",
            progress=0.01,
            detail="任务已开始",
            waiting_reason="",
            started_at=datetime.now().astimezone().isoformat(),
        )
        try:
            call_kwargs = dict(kwargs)
            try:
                accepts_progress = "progress_callback" in inspect.signature(function).parameters
            except (TypeError, ValueError):
                accepts_progress = False
            if accepts_progress and "progress_callback" not in call_kwargs:
                call_kwargs["progress_callback"] = (
                    lambda **values: self._update(job_id, **values)
                )
            result = function(*args, **call_kwargs)
            self._update(
                job_id,
                status="SUCCEEDED",
                phase="COMPLETED",
                progress=1.0,
                detail="任务完成",
                waiting_reason="",
                result=result,
                finished_at=datetime.now().astimezone().isoformat(),
            )
        except Exception as exc:
            self._update(
                job_id,
                status="FAILED",
                phase="FAILED",
                detail="任务失败",
                waiting_reason="",
                error=str(exc),
                finished_at=datetime.now().astimezone().isoformat(),
            )
        finally:
            with self._lock:
                fingerprint = str(self._jobs.get(job_id, {}).get("fingerprint") or "")
                if self._active_fingerprints.get(fingerprint) == job_id:
                    self._active_fingerprints.pop(fingerprint, None)

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return self._jobs[job_id].copy()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (item.copy() for item in self._jobs.values()), key=lambda item: item["created_at"], reverse=True
            )

    def active(self, job_type: str) -> dict[str, Any] | None:
        with self._lock:
            matches = [
                item
                for item in self._jobs.values()
                if item["job_type"] == job_type and item["status"] in ("QUEUED", "RUNNING")
            ]
            if not matches:
                return None
            return max(matches, key=lambda item: item["created_at"]).copy()

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)


def _job_fingerprint(
    job_type: str,
    function: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    function_name = f"{getattr(function, '__module__', '')}.{getattr(function, '__qualname__', repr(function))}"
    return json.dumps(
        [job_type, function_name, args, kwargs],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
