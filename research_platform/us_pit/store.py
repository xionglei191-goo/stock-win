from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import pandas as pd

from .hashing import canonical_json_bytes, sha256_bytes, sha256_file, sha256_json
from .models import (
    ArtifactDescriptor,
    EvidenceAuthority,
    ObjectRef,
    OverrideApproval,
    OverrideProposal,
    QualityReport,
    ReleaseManifest,
    ReleaseStatus,
    SourceDependency,
)


PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
JSON_MEDIA_TYPE = "application/json"
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


def _mark_read_only(path: Path) -> None:
    path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _mark_writable(path: Path) -> None:
    path.chmod(stat.S_IREAD | stat.S_IWRITE)


def _atomic_write_mutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


@dataclass(frozen=True)
class SourceBatch:
    batch_id: str
    dependencies: tuple[SourceDependency, ...]
    path: Path


@dataclass(frozen=True)
class OverrideState:
    proposal: OverrideProposal
    draft_sha256: str
    approval: OverrideApproval | None

    @property
    def approved(self) -> bool:
        return self.approval is not None and self.approval.draft_sha256 == self.draft_sha256


class USPITRelease:
    """Verified view of one immutable release directory."""

    def __init__(self, path: Path, manifest: ReleaseManifest) -> None:
        self.path = path
        self.manifest = manifest

    @property
    def release_id(self) -> str:
        return self.manifest.release_id

    @property
    def universe_id(self) -> str:
        return self.manifest.universe_id

    @property
    def status(self) -> ReleaseStatus:
        return self.manifest.status

    @property
    def quality_report(self) -> QualityReport:
        descriptor = self.manifest.artifacts.get("quality_report")
        if descriptor is None:
            raise ValueError("release has no quality_report artifact")
        payload = json.loads(self.artifact_path("quality_report").read_text(encoding="utf-8"))
        return QualityReport.from_dict(payload)

    @property
    def includes_delisted(self) -> bool:
        return self.quality_report.includes_delisted

    def artifact_path(self, name: str) -> Path:
        descriptor = self.manifest.artifacts.get(name)
        if descriptor is None:
            raise KeyError(f"release artifact not found: {name}")
        path = self.path / descriptor.filename
        if path.parent.resolve() != self.path.resolve():
            raise ValueError(f"unsafe artifact filename: {descriptor.filename}")
        if not path.is_file():
            raise ValueError(f"release artifact is missing: {name}")
        if sha256_file(path) != descriptor.object_sha256:
            raise ValueError(f"release artifact hash mismatch: {name}")
        return path

    def load_frame(self, name: str) -> pd.DataFrame:
        descriptor = self.manifest.artifacts.get(name)
        if descriptor is None:
            raise KeyError(f"release artifact not found: {name}")
        if descriptor.media_type != PARQUET_MEDIA_TYPE:
            raise ValueError(f"release artifact is not parquet: {name}")
        return pd.read_parquet(self.artifact_path(name))

    def verify(self) -> None:
        if self.path.name != self.release_id:
            raise ValueError("release directory identity mismatch")
        for name in self.manifest.artifacts:
            self.artifact_path(name)
        report = self.quality_report
        if report.status != self.status:
            raise ValueError("manifest and quality report status mismatch")
        if report.policy_version != self.manifest.quality_policy_version:
            raise ValueError("manifest and quality policy version mismatch")

    def to_backtest_dataset(self) -> "USBacktestDataset":
        from .dataset import USBacktestDataset

        return USBacktestDataset.from_release(self)


class USPITStore:
    """Content-addressed evidence and append-only release storage."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "raw" / "sha256"
        self.batches_dir = self.root / "raw" / "batches"
        self.releases_dir = self.root / "releases"
        self.staging_dir = self.releases_dir / ".staging"
        self.override_dir = self.root / "overrides"
        self.catalog_path = self.root / "catalog.sqlite3"
        for path in (
            self.objects_dir,
            self.batches_dir,
            self.releases_dir,
            self.staging_dir,
            self.override_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._initialize_catalog()

    def _connect_catalog(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.catalog_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_catalog(self) -> None:
        with closing(self._connect_catalog()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS us_pit_source_batches (
                    batch_id TEXT PRIMARY KEY,
                    batch_sha256 TEXT NOT NULL,
                    dependency_count INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    source_lineage_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS us_pit_releases (
                    release_id TEXT PRIMARY KEY,
                    manifest_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    universe_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    quality_policy_version TEXT NOT NULL,
                    format_version TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    gate_report_artifact TEXT,
                    source_lineage_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS us_pit_release_artifacts (
                    release_id TEXT NOT NULL,
                    artifact_name TEXT NOT NULL,
                    object_sha256 TEXT NOT NULL,
                    schema_sha256 TEXT,
                    row_count INTEGER,
                    path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    PRIMARY KEY (release_id, artifact_name),
                    FOREIGN KEY (release_id)
                        REFERENCES us_pit_releases(release_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_us_pit_releases_status
                    ON us_pit_releases(status, universe_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_us_pit_artifacts_sha256
                    ON us_pit_release_artifacts(object_sha256);
                """
            )

    @staticmethod
    def _registered_at() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _catalog_relative_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError(f"catalog path is outside the PIT root: {path}") from exc
        return relative.as_posix()

    def _register_source_batch(self, batch: SourceBatch) -> None:
        batch_sha256 = sha256_file(batch.path)
        lineage_json = canonical_json_bytes(
            [dependency.to_dict() for dependency in batch.dependencies]
        ).decode("utf-8")
        immutable = {
            "batch_id": batch.batch_id,
            "batch_sha256": batch_sha256,
            "dependency_count": len(batch.dependencies),
            "path": self._catalog_relative_path(batch.path),
            "source_lineage_json": lineage_json,
        }
        with closing(self._connect_catalog()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT batch_id, batch_sha256, dependency_count, path,
                       source_lineage_json
                FROM us_pit_source_batches
                WHERE batch_id = ?
                """,
                (batch.batch_id,),
            ).fetchone()
            if existing is not None:
                if dict(existing) != immutable:
                    raise ValueError(
                        f"PIT catalog source batch conflict: {batch.batch_id}"
                    )
                return
            connection.execute(
                """
                INSERT INTO us_pit_source_batches (
                    batch_id, batch_sha256, dependency_count, path,
                    source_lineage_json, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    immutable["batch_id"],
                    immutable["batch_sha256"],
                    immutable["dependency_count"],
                    immutable["path"],
                    immutable["source_lineage_json"],
                    self._registered_at(),
                ),
            )

    def _register_release(self, release: USPITRelease) -> None:
        manifest = release.manifest
        manifest_path = release.path / "manifest.json"
        source_lineage_json = canonical_json_bytes(
            manifest.identity_payload()["sources"]
        ).decode("utf-8")
        quality_descriptor = manifest.artifacts.get("quality_report")
        gate_report_artifact: str | None = None
        if quality_descriptor is not None:
            gate_report_artifact = canonical_json_bytes(
                {
                    "name": quality_descriptor.name,
                    "object_sha256": quality_descriptor.object_sha256,
                    "path": self._catalog_relative_path(
                        release.path / quality_descriptor.filename
                    ),
                }
            ).decode("utf-8")
        immutable_release = {
            "release_id": manifest.release_id,
            "manifest_sha256": sha256_file(manifest_path),
            "status": manifest.status.value,
            "universe_id": manifest.universe_id,
            "created_at": manifest.created_at,
            "quality_policy_version": manifest.quality_policy_version,
            "format_version": manifest.format_version,
            "manifest_path": self._catalog_relative_path(manifest_path),
            "gate_report_artifact": gate_report_artifact,
            "source_lineage_json": source_lineage_json,
        }
        immutable_artifacts = {
            name: {
                "release_id": manifest.release_id,
                "artifact_name": name,
                "object_sha256": descriptor.object_sha256,
                "schema_sha256": descriptor.schema_sha256,
                "row_count": descriptor.row_count,
                "path": self._catalog_relative_path(
                    release.path / descriptor.filename
                ),
                "media_type": descriptor.media_type,
                "size_bytes": descriptor.size_bytes,
            }
            for name, descriptor in manifest.artifacts.items()
        }
        with closing(self._connect_catalog()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT release_id, manifest_sha256, status, universe_id,
                       created_at, quality_policy_version, format_version,
                       manifest_path, gate_report_artifact, source_lineage_json
                FROM us_pit_releases
                WHERE release_id = ?
                """,
                (manifest.release_id,),
            ).fetchone()
            if existing is not None and dict(existing) != immutable_release:
                raise ValueError(
                    f"PIT catalog release conflict: {manifest.release_id}"
                )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO us_pit_releases (
                        release_id, manifest_sha256, status, universe_id,
                        created_at, quality_policy_version, format_version,
                        manifest_path, gate_report_artifact,
                        source_lineage_json, registered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        immutable_release["release_id"],
                        immutable_release["manifest_sha256"],
                        immutable_release["status"],
                        immutable_release["universe_id"],
                        immutable_release["created_at"],
                        immutable_release["quality_policy_version"],
                        immutable_release["format_version"],
                        immutable_release["manifest_path"],
                        immutable_release["gate_report_artifact"],
                        immutable_release["source_lineage_json"],
                        self._registered_at(),
                    ),
                )

            rows = connection.execute(
                """
                SELECT release_id, artifact_name, object_sha256,
                       schema_sha256, row_count, path, media_type, size_bytes
                FROM us_pit_release_artifacts
                WHERE release_id = ?
                """,
                (manifest.release_id,),
            ).fetchall()
            existing_artifacts = {
                str(row["artifact_name"]): dict(row) for row in rows
            }
            if set(existing_artifacts) - set(immutable_artifacts):
                raise ValueError(
                    f"PIT catalog release artifact conflict: {manifest.release_id}"
                )
            for name, artifact in immutable_artifacts.items():
                existing_artifact = existing_artifacts.get(name)
                if existing_artifact is not None:
                    if existing_artifact != artifact:
                        raise ValueError(
                            "PIT catalog release artifact conflict: "
                            f"{manifest.release_id}/{name}"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO us_pit_release_artifacts (
                        release_id, artifact_name, object_sha256,
                        schema_sha256, row_count, path, media_type, size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact["release_id"],
                        artifact["artifact_name"],
                        artifact["object_sha256"],
                        artifact["schema_sha256"],
                        artifact["row_count"],
                        artifact["path"],
                        artifact["media_type"],
                        artifact["size_bytes"],
                    ),
                )

    def object_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid SHA-256 digest")
        return self.objects_dir / digest[:2] / digest

    def put_bytes(
        self,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
        row_count: int | None = None,
        schema_sha256: str | None = None,
    ) -> ObjectRef:
        digest = sha256_bytes(payload)
        target = self.object_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_file(target) != digest:
                raise ValueError(f"content-addressed object is corrupt: {digest}")
        else:
            temporary = target.with_name(f".{digest}.{uuid4().hex}.tmp")
            temporary.write_bytes(payload)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if sha256_file(target) != digest:
                    raise ValueError(f"content-addressed object collision: {digest}")
            finally:
                temporary.unlink(missing_ok=True)
            _mark_read_only(target)
        return ObjectRef(
            sha256=digest,
            size_bytes=len(payload),
            media_type=media_type,
            path=target,
            row_count=row_count,
            schema_sha256=schema_sha256,
        )

    def put_file(
        self,
        path: Path | str,
        *,
        media_type: str = "application/octet-stream",
        row_count: int | None = None,
        schema_sha256: str | None = None,
    ) -> ObjectRef:
        source = Path(path)
        digest = sha256_file(source)
        target = self.object_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_file(target) != digest:
                raise ValueError(f"content-addressed object is corrupt: {digest}")
        else:
            temporary = target.with_name(f".{digest}.{uuid4().hex}.tmp")
            shutil.copyfile(source, temporary)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if sha256_file(target) != digest:
                    raise ValueError(f"content-addressed object collision: {digest}")
            finally:
                temporary.unlink(missing_ok=True)
            _mark_read_only(target)
        return ObjectRef(
            sha256=digest,
            size_bytes=source.stat().st_size,
            media_type=media_type,
            path=target,
            row_count=row_count,
            schema_sha256=schema_sha256,
        )

    def put_dataframe(self, frame: pd.DataFrame) -> ObjectRef:
        schema_payload = [
            {"name": str(name), "dtype": str(dtype)}
            for name, dtype in zip(frame.columns, frame.dtypes, strict=True)
        ]
        schema_sha256 = sha256_json(schema_payload)
        with tempfile.NamedTemporaryFile(
            dir=self.root,
            suffix=".parquet",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        try:
            frame.to_parquet(temporary, index=False)
            return self.put_file(
                temporary,
                media_type=PARQUET_MEDIA_TYPE,
                row_count=len(frame),
                schema_sha256=schema_sha256,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def write_source_batch(self, dependencies: Iterable[SourceDependency]) -> SourceBatch:
        ordered = tuple(
            sorted(
                dependencies,
                key=lambda item: (
                    item.source_id,
                    item.dataset,
                    item.as_of_date or "",
                    item.object_sha256,
                ),
            )
        )
        payload_value = {
            "format_version": "us-pit-source-batch-v1",
            "dependencies": [item.to_dict() for item in ordered],
        }
        payload = canonical_json_bytes(payload_value)
        batch_id = sha256_bytes(payload)
        reference = self.put_bytes(payload, media_type=JSON_MEDIA_TYPE)
        target = self.batches_dir / f"{batch_id}.json"
        if target.exists():
            if sha256_file(target) != reference.sha256:
                raise ValueError(f"source batch is corrupt: {batch_id}")
        else:
            shutil.copy2(reference.path, target)
            _mark_read_only(target)
        batch = SourceBatch(batch_id=batch_id, dependencies=ordered, path=target)
        self._register_source_batch(batch)
        return batch

    def load_source_batch(self, batch_id: str) -> SourceBatch:
        """Load a captured source batch after verifying its content address."""

        if len(batch_id) != 64 or any(
            character not in "0123456789abcdef" for character in batch_id
        ):
            raise ValueError("batch_id must be a lowercase SHA-256 digest")
        path = self.batches_dir / f"{batch_id}.json"
        if not path.is_file():
            raise ValueError(f"source batch not found: {batch_id}")
        payload = path.read_bytes()
        if sha256_bytes(payload) != batch_id:
            raise ValueError(f"source batch is corrupt: {batch_id}")
        value = json.loads(payload)
        dependencies = tuple(
            SourceDependency.from_dict(item)
            for item in value.get("dependencies", [])
        )
        if not dependencies:
            raise ValueError(f"source batch contains no dependencies: {batch_id}")
        batch = SourceBatch(batch_id=batch_id, dependencies=dependencies, path=path)
        self._register_source_batch(batch)
        return batch

    def list_source_batches(self) -> tuple[SourceBatch, ...]:
        """Return every hash-verified captured source batch."""

        return tuple(
            self.load_source_batch(path.stem)
            for path in sorted(self.batches_dir.glob("*.json"))
        )

    def descriptor(self, name: str, reference: ObjectRef) -> ArtifactDescriptor:
        if not _SAFE_NAME.fullmatch(name):
            raise ValueError(f"invalid artifact name: {name}")
        suffix = ".parquet" if reference.media_type == PARQUET_MEDIA_TYPE else ".json"
        return ArtifactDescriptor(
            name=name,
            filename=f"{name}{suffix}",
            object_sha256=reference.sha256,
            size_bytes=reference.size_bytes,
            media_type=reference.media_type,
            row_count=reference.row_count,
            schema_sha256=reference.schema_sha256,
        )

    def publish_release(
        self,
        manifest: ReleaseManifest,
        objects: Mapping[str, ObjectRef],
    ) -> USPITRelease:
        if set(objects) != set(manifest.artifacts):
            raise ValueError("manifest artifacts and supplied objects differ")
        release_id = manifest.release_id
        final = self.releases_dir / release_id
        if final.exists():
            existing = self.load_release(release_id)
            if existing.manifest.identity_payload() != manifest.identity_payload():
                raise ValueError(f"immutable release collision: {release_id}")
            return existing

        staging = self.staging_dir / f"{release_id}.{uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            for name, descriptor in manifest.artifacts.items():
                reference = objects[name]
                if reference.sha256 != descriptor.object_sha256:
                    raise ValueError(f"object hash does not match descriptor: {name}")
                if sha256_file(reference.path) != reference.sha256:
                    raise ValueError(f"source object is corrupt: {name}")
                destination = staging / descriptor.filename
                # Release copies deliberately do not hard-link the raw CAS.  A
                # local permission override on one release must never corrupt
                # the only preserved copy of its source evidence.
                shutil.copy2(reference.path, destination)
                _mark_read_only(destination)

            manifest_path = staging / "manifest.json"
            manifest_path.write_bytes(canonical_json_bytes(manifest.to_dict()))
            _mark_read_only(manifest_path)
            try:
                os.rename(staging, final)
            except FileExistsError:
                shutil.rmtree(staging, ignore_errors=True)
                return self.load_release(release_id)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        release = self.load_release(release_id)
        release.verify()
        return release

    def load_release(self, release_id: str) -> USPITRelease:
        if len(release_id) != 64:
            raise ValueError("release_id must be a SHA-256 digest")
        path = self.releases_dir / release_id
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"PIT release not found: {release_id}")
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = ReleaseManifest.from_dict(value)
        if manifest.release_id != release_id:
            raise ValueError("release_id does not match manifest")
        release = USPITRelease(path, manifest)
        release.verify()
        self._register_release(release)
        return release

    def list_releases(self) -> tuple[USPITRelease, ...]:
        releases: list[USPITRelease] = []
        for path in sorted(self.releases_dir.iterdir()):
            if path.is_dir() and len(path.name) == 64:
                releases.append(self.load_release(path.name))
        return tuple(releases)

    def propose_override(self, proposal: OverrideProposal) -> OverrideState:
        if not _SAFE_NAME.fullmatch(proposal.override_id):
            raise ValueError("override_id must be a safe lowercase identifier")
        if not proposal.reason.strip() or not proposal.proposed_by.strip():
            raise ValueError("override reason and proposed_by are required")
        if not proposal.evidence:
            raise ValueError("override evidence is required")
        for evidence in proposal.evidence:
            captured = self.object_path(evidence.content_sha256)
            if not captured.is_file() or sha256_file(captured) != evidence.content_sha256:
                raise ValueError(
                    f"override evidence is not captured in the content store: "
                    f"{evidence.source_id}"
                )
        datetime_value = datetime.fromisoformat(proposal.proposed_at.replace("Z", "+00:00"))
        if datetime_value.tzinfo is None:
            raise ValueError("override proposed_at must be timezone-aware")

        revisions = self.override_dir / "drafts" / proposal.override_id
        revisions.mkdir(parents=True, exist_ok=True)
        payload = canonical_json_bytes(proposal.to_dict())
        draft_hash = sha256_bytes(payload)
        revision_path = revisions / f"{draft_hash}.json"
        if not revision_path.exists():
            revision_path.write_bytes(payload)
            _mark_read_only(revision_path)
        pointer = self.override_dir / "current" / f"{proposal.override_id}.json"
        _atomic_write_mutable(pointer, canonical_json_bytes({"draft_sha256": draft_hash}))
        return OverrideState(proposal, draft_hash, self._load_approval(proposal.override_id, draft_hash))

    def approve_override(
        self,
        override_id: str,
        *,
        expected_sha256: str,
        approved_at: str,
        approved_by: str,
        acknowledgement: str,
    ) -> OverrideState:
        state = self.get_override(override_id)
        if state.draft_sha256 != expected_sha256:
            raise ValueError("override draft hash changed; approval rejected")
        approved_time = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
        if approved_time.tzinfo is None:
            raise ValueError("override approved_at must be timezone-aware")
        proposed_time = datetime.fromisoformat(
            state.proposal.proposed_at.replace("Z", "+00:00")
        )
        if approved_time < proposed_time:
            raise ValueError("override approval cannot predate its proposal")
        if not approved_by.strip() or not acknowledgement.strip():
            raise ValueError("local approver and acknowledgement are required")
        authoritative = any(
            item.authority == EvidenceAuthority.AUTHORITATIVE_PRIMARY
            for item in state.proposal.evidence
        )
        independent_urls = {item.url for item in state.proposal.evidence if item.url}
        if not authoritative and len(independent_urls) < 2:
            raise ValueError(
                "override approval requires one authoritative primary source "
                "or two independent evidence URLs"
            )
        approval = OverrideApproval(
            override_id=override_id,
            draft_sha256=expected_sha256,
            approved_at=approved_at,
            approved_by=approved_by,
            acknowledgement=acknowledgement,
        )
        target = self.override_dir / "approvals" / override_id / f"{expected_sha256}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = canonical_json_bytes(approval.to_dict())
        if target.exists() and target.read_bytes() != payload:
            raise ValueError("immutable override approval already exists with different content")
        if not target.exists():
            target.write_bytes(payload)
            _mark_read_only(target)
        return OverrideState(state.proposal, state.draft_sha256, approval)

    def get_override(self, override_id: str) -> OverrideState:
        if not _SAFE_NAME.fullmatch(override_id):
            raise ValueError("override_id must be a safe lowercase identifier")
        pointer = self.override_dir / "current" / f"{override_id}.json"
        if not pointer.is_file():
            raise ValueError(f"override draft not found: {override_id}")
        pointer_value = json.loads(pointer.read_text(encoding="utf-8"))
        draft_hash = str(pointer_value["draft_sha256"])
        revision = self.override_dir / "drafts" / override_id / f"{draft_hash}.json"
        payload = revision.read_bytes()
        if sha256_bytes(payload) != draft_hash:
            raise ValueError(f"override draft is corrupt: {override_id}")
        proposal = OverrideProposal.from_dict(json.loads(payload))
        return OverrideState(proposal, draft_hash, self._load_approval(override_id, draft_hash))

    def list_overrides(self) -> tuple[OverrideState, ...]:
        current = self.override_dir / "current"
        if not current.exists():
            return ()
        return tuple(self.get_override(path.stem) for path in sorted(current.glob("*.json")))

    def _load_approval(self, override_id: str, draft_hash: str) -> OverrideApproval | None:
        path = self.override_dir / "approvals" / override_id / f"{draft_hash}.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return OverrideApproval(
            override_id=str(value["override_id"]),
            draft_sha256=str(value["draft_sha256"]),
            approved_at=str(value["approved_at"]),
            approved_by=str(value["approved_by"]),
            acknowledgement=str(value["acknowledgement"]),
        )


__all__ = [
    "JSON_MEDIA_TYPE",
    "PARQUET_MEDIA_TYPE",
    "OverrideState",
    "SourceBatch",
    "USPITRelease",
    "USPITStore",
]
