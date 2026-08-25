from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

import exchange_calendars as xcals
import pandas as pd

from .alias_crosscheck import crosscheck_current_aliases
from .hashing import canonical_json_bytes, sha256_bytes, sha256_file
from .service import USPITService
from .sources import SourceAdapter, SyncRequest
from .sources_official import ISharesIVVObservedSnapshotAdapter
from .tdx_current_master import TDXCurrentUSMasterAdapter


FORMAT_VERSION = "us-pit-forward-capture-v1"
NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_TASK_NAME = "ResearchPlatform-USPIT-ForwardCapture"


@dataclass(frozen=True)
class ForwardCaptureResult:
    status: str
    session_date: date
    receipt: dict[str, Any]
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session_date": self.session_date.isoformat(),
            "path": None if self.path is None else str(self.path),
            **self.receipt,
            "broker_writes_enabled": False,
        }


class USPITForwardCaptureService:
    """Append-only post-close capture of current official and TDX evidence.

    The service never backfills a missed session. A capture is admitted only
    on the actual New York calendar date after the XNYS regular close plus the
    configured delay. Repeated calls return the hash-verified receipt without
    issuing another source request.
    """

    def __init__(
        self,
        pit: USPITService | Path | str,
        *,
        close_delay: timedelta = timedelta(minutes=15),
    ) -> None:
        self.pit = pit if isinstance(pit, USPITService) else USPITService(pit)
        self.root = self.pit.store.root / "forward_captures"
        self.alias_root = self.pit.store.root / "alias_crosschecks" / "forward"
        self.staging = self.root / ".staging"
        self.root.mkdir(parents=True, exist_ok=True)
        self.alias_root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.close_delay = close_delay
        if close_delay < timedelta(0):
            raise ValueError("close_delay cannot be negative")

    def capture(
        self,
        *,
        observed_at: datetime | None = None,
        ishares_adapter: SourceAdapter | None = None,
        tdx_adapter: SourceAdapter | None = None,
    ) -> ForwardCaptureResult:
        observed = observed_at or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        observed = observed.astimezone(timezone.utc)
        session_date, gate = self._capture_gate(observed)
        if gate is not None:
            return ForwardCaptureResult(gate, session_date, {})

        target = self.root / session_date.isoformat()
        if target.exists():
            return self._load_existing(target, session_date)

        request = SyncRequest(session_date, session_date, observed)
        ishares_batch = self.pit.sync(
            ishares_adapter or ISharesIVVObservedSnapshotAdapter(), request
        )
        tdx_batch = self.pit.sync(
            tdx_adapter or TDXCurrentUSMasterAdapter(), request
        )
        official_batch_ids = self._official_batch_ids(ishares_batch.batch_id)
        normalization = self.pit.normalize_official_evidence(official_batch_ids)
        alias_target = self.alias_root / (
            f"{session_date.isoformat()}-"
            f"{normalization.normalization_id[:12]}-{tdx_batch.batch_id[:12]}"
        )
        if alias_target.exists():
            alias_manifest = self._load_alias_manifest(alias_target)
        else:
            alias_manifest = crosscheck_current_aliases(
                self.pit.store,
                normalization.path,
                tdx_batch.batch_id,
                alias_target,
            ).manifest

        receipt = {
            "format_version": FORMAT_VERSION,
            "session_date": session_date.isoformat(),
            "observed_at": observed.isoformat(),
            "ishares_source_batch_id": ishares_batch.batch_id,
            "tdx_source_batch_id": tdx_batch.batch_id,
            "official_source_batch_ids": list(official_batch_ids),
            "normalization_id": normalization.normalization_id,
            "normalization_manifest_sha256": sha256_file(
                normalization.path / "manifest.json"
            ),
            "alias_crosscheck_id": alias_manifest["crosscheck_id"],
            "alias_crosscheck_manifest_sha256": sha256_file(
                alias_target / "manifest.json"
            ),
            "alias_verified_count": int(alias_manifest["verified_count"]),
            "alias_row_count": int(alias_manifest["row_count"]),
            "current_only": True,
            "historical_membership_authority": False,
            "backfill_performed": False,
        }
        receipt["capture_id"] = sha256_bytes(canonical_json_bytes(receipt))
        self._publish_receipt(target, receipt)
        return ForwardCaptureResult("CAPTURED", session_date, receipt, target)

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        receipts = sorted(
            path
            for path in self.root.iterdir()
            if path.is_dir() and path.name != ".staging"
        )
        latest: dict[str, Any] | None = None
        if receipts:
            result = self._load_existing(
                receipts[-1], date.fromisoformat(receipts[-1].name)
            )
            latest = result.to_dict()
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        session_date, gate = self._capture_gate(current.astimezone(timezone.utc))
        return {
            "status": "CAPTURED" if latest else "NO_FORWARD_CAPTURE",
            "latest": latest,
            "current_session_date": session_date.isoformat(),
            "current_gate": gate or "ELIGIBLE_POST_CLOSE",
            "capture_count": len(receipts),
            "historical_backfill_allowed": False,
            "broker_writes_enabled": False,
        }

    def latest_alias_status(self) -> dict[str, Any]:
        manifests: list[tuple[pd.Timestamp, Path, dict[str, Any]]] = []
        root = self.pit.store.root / "alias_crosschecks"
        if root.is_dir():
            for path in root.rglob("manifest.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if value.get("format_version") != "us-pit-current-alias-crosscheck-v1":
                        continue
                    observed = pd.Timestamp(value["selected_observed_at"])
                    manifests.append((observed, path, value))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        if not manifests:
            return {
                "status": "NO_CURRENT_ALIAS_CROSSCHECK",
                "current_only": True,
                "historical_alias_authority": False,
            }
        _observed, path, value = max(manifests, key=lambda item: item[0])
        return {
            "status": value["status"],
            "selected_observed_at": value["selected_observed_at"],
            "verified_count": int(value["verified_count"]),
            "row_count": int(value["row_count"]),
            "manifest_sha256": sha256_file(path),
            "path": str(path.parent),
            "current_only": True,
            "historical_alias_authority": False,
        }

    def _capture_gate(self, observed: datetime) -> tuple[date, str | None]:
        current_ny = observed.astimezone(NEW_YORK)
        session_date = current_ny.date()
        calendar = xcals.get_calendar("XNYS")
        label = pd.Timestamp(session_date)
        if not calendar.is_session(label):
            return session_date, "MARKET_CLOSED"
        close = calendar.session_close(label).to_pydatetime().astimezone(timezone.utc)
        if observed < close + self.close_delay:
            return session_date, "WAITING_FOR_POST_CLOSE"
        return session_date, None

    def _official_batch_ids(self, current_ishares_batch_id: str) -> tuple[str, ...]:
        batch = self.pit.store.load_source_batch(current_ishares_batch_id)
        if not any(
            dependency.dataset == "fund_holdings_observed"
            and dependency.source_id == "ishares_ivv_holdings"
            and dict(dependency.metadata).get("artifact_kind")
            == "raw_observed_holdings_csv"
            for dependency in batch.dependencies
        ):
            raise ValueError("forward capture batch has no current observed iShares holdings")
        return (current_ishares_batch_id,)

    def _load_existing(self, target: Path, session_date: date) -> ForwardCaptureResult:
        receipt_path = target / "receipt.json"
        if target.name != session_date.isoformat() or not receipt_path.is_file():
            raise ValueError("forward capture receipt path is invalid")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        supplied_id = str(receipt.get("capture_id") or "")
        identity = dict(receipt)
        identity.pop("capture_id", None)
        if supplied_id != sha256_bytes(canonical_json_bytes(identity)):
            raise ValueError("forward capture receipt identity mismatch")
        for key in ("ishares_source_batch_id", "tdx_source_batch_id"):
            self.pit.store.load_source_batch(str(receipt[key]))
        normalization_path = (
            self.pit.store.root
            / "normalized"
            / "official"
            / str(receipt["normalization_id"])
        )
        if sha256_file(normalization_path / "manifest.json") != receipt[
            "normalization_manifest_sha256"
        ]:
            raise ValueError("forward capture normalization hash mismatch")
        alias_candidates = list(
            self.alias_root.glob(f"{session_date.isoformat()}-*/manifest.json")
        )
        matches = [
            path
            for path in alias_candidates
            if sha256_file(path) == receipt["alias_crosscheck_manifest_sha256"]
        ]
        if len(matches) != 1:
            raise ValueError("forward capture alias cross-check is missing or ambiguous")
        return ForwardCaptureResult("ALREADY_CAPTURED", session_date, receipt, target)

    @staticmethod
    def _load_alias_manifest(target: Path) -> dict[str, Any]:
        path = target / "manifest.json"
        if not path.is_file():
            raise ValueError("existing alias cross-check has no manifest")
        return json.loads(path.read_text(encoding="utf-8"))

    def _publish_receipt(self, target: Path, receipt: dict[str, Any]) -> None:
        stage = self.staging / f"{target.name}.{uuid4().hex}"
        stage.mkdir(parents=False, exist_ok=False)
        try:
            path = stage / "receipt.json"
            path.write_bytes(canonical_json_bytes(receipt))
            path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            try:
                os.rename(stage, target)
            except FileExistsError:
                existing = self._load_existing(target, date.fromisoformat(target.name))
                if existing.receipt != receipt:
                    raise ValueError(
                        "concurrent forward capture produced conflicting evidence"
                    )
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)


def forward_capture_task_spec(
    *,
    python_executable: str,
    project_root: Path | str,
    task_name: str = DEFAULT_TASK_NAME,
) -> dict[str, Any]:
    executable = str(python_executable).strip()
    root = Path(project_root).resolve()
    name = _task_name(task_name)
    if not executable or not root.is_dir():
        raise ValueError("python executable and existing project root are required")
    return {
        "task_name": name,
        "trigger": {"type": "DAILY_REPEATING", "interval_seconds": 300},
        "action": {
            "program": executable,
            "arguments": "-m research_platform us-pit capture-current",
            "working_directory": str(root),
        },
        "read_only_market_data": True,
        "historical_backfill_allowed": False,
        "broker_writes_enabled": False,
    }


def install_forward_capture_task(
    spec: dict[str, Any],
    *,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    normalized = _validate_task_spec(spec)
    action = normalized["action"]
    start = datetime.now().astimezone().replace(microsecond=0).isoformat()
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Append-only US PIT post-close capture; read-only market data.</Description></RegistrationInfo>
  <Triggers><TimeTrigger><Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>{escape(start)}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled><ExecutionTimeLimit>PT10M</ExecutionTimeLimit></Settings>
  <Actions Context="Author"><Exec><Command>{escape(action['program'])}</Command><Arguments>{escape(action['arguments'])}</Arguments><WorkingDirectory>{escape(action['working_directory'])}</WorkingDirectory></Exec></Actions>
</Task>"""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-16", suffix=".xml", delete=False
        ) as handle:
            handle.write(xml)
            temporary = Path(handle.name)
        completed = runner(
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                normalized["task_name"],
                "/XML",
                str(temporary),
                "/F",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "forward capture Task Scheduler registration failed: "
            + str(completed.stderr or completed.stdout).strip()
        )
    return {**normalized, "registered": True}


def forward_capture_task_status(
    task_name: str = DEFAULT_TASK_NAME,
    *,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    name = _task_name(task_name)
    completed = runner(
        ["schtasks.exe", "/Query", "/TN", name, "/FO", "LIST", "/V"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "task_name": name,
        "registered": completed.returncode == 0,
        "scheduler_output": str(completed.stdout or completed.stderr or "").strip(),
        "read_only_market_data": True,
        "broker_writes_enabled": False,
    }


def remove_forward_capture_task(
    task_name: str = DEFAULT_TASK_NAME,
    *,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    before = forward_capture_task_status(task_name, runner=runner)
    if not before["registered"]:
        return {**before, "removed": False, "reason": "TASK_NOT_REGISTERED"}
    completed = runner(
        ["schtasks.exe", "/Delete", "/TN", before["task_name"], "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "forward capture Task Scheduler removal failed: "
            + str(completed.stderr or completed.stdout).strip()
        )
    return {**before, "registered": False, "removed": True}


def _task_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 120 or any(char in name for char in "\r\n\0"):
        raise ValueError("Task Scheduler task_name is invalid")
    return name


def _validate_task_spec(spec: dict[str, Any]) -> dict[str, Any]:
    action = spec.get("action")
    trigger = spec.get("trigger")
    if not isinstance(action, dict) or not isinstance(trigger, dict):
        raise ValueError("forward capture task action and trigger are required")
    if int(trigger.get("interval_seconds") or 0) != 300:
        raise ValueError("forward capture worker interval is fixed at 300 seconds")
    arguments = str(action.get("arguments") or "")
    if arguments != "-m research_platform us-pit capture-current":
        raise ValueError("Task Scheduler action is not the read-only PIT capture worker")
    root = Path(str(action.get("working_directory") or "")).resolve()
    if not root.is_dir():
        raise ValueError("Task Scheduler working directory does not exist")
    return {
        **spec,
        "task_name": _task_name(spec.get("task_name")),
        "action": {
            "program": str(action.get("program") or "").strip(),
            "arguments": arguments,
            "working_directory": str(root),
        },
        "read_only_market_data": True,
        "historical_backfill_allowed": False,
        "broker_writes_enabled": False,
    }


__all__ = [
    "DEFAULT_TASK_NAME",
    "ForwardCaptureResult",
    "USPITForwardCaptureService",
    "forward_capture_task_spec",
    "forward_capture_task_status",
    "install_forward_capture_task",
    "remove_forward_capture_task",
]
