from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

import pandas as pd

from .hashing import canonical_json_bytes, sha256_file
from .models import (
    ObjectRef,
    ReleaseManifest,
    SourceDependency,
    UNIVERSE_ID,
)
from .quality import REQUIRED_ARTIFACT_COLUMNS, USPITQualityValidator
from .sources import SourceAdapter, SyncRequest
from .store import JSON_MEDIA_TYPE, PARQUET_MEDIA_TYPE, SourceBatch, USPITRelease, USPITStore


class USPITService:
    """Orchestrates evidence capture, release assembly, and fail-closed validation."""

    def __init__(
        self,
        store: USPITStore | Path | str,
        validator: USPITQualityValidator | None = None,
    ) -> None:
        self.store = store if isinstance(store, USPITStore) else USPITStore(store)
        self.validator = validator or USPITQualityValidator()

    def list_releases(self) -> list[dict[str, Any]]:
        """Return verified, JSON-safe summaries for the read-only catalog."""

        return [self._release_summary(release) for release in self.store.list_releases()]

    def list_source_batches(self) -> list[dict[str, Any]]:
        return [
            {
                "batch_id": batch.batch_id,
                "dependency_count": len(batch.dependencies),
                "sources": [item.to_dict() for item in batch.dependencies],
            }
            for batch in self.store.list_source_batches()
        ]

    def capture_official_evidence(
        self,
        request: SyncRequest,
        *,
        sec_user_agent: str | None = None,
    ) -> tuple[SourceBatch, ...]:
        """Freeze the approved official anchor/current-observation sources."""

        from .sources_official import (
            ISharesIVVObservedSnapshotAdapter,
            SECNPortIVVAdapter,
        )

        return (
            self.sync(SECNPortIVVAdapter(user_agent=sec_user_agent), request),
            self.sync(ISharesIVVObservedSnapshotAdapter(), request),
        )

    def normalize_official_evidence(
        self,
        source_batch_ids: Iterable[str],
    ) -> "OfficialNormalizationResult":
        """Parse captured official objects into a review-only candidate package."""

        from .official_normalize import OfficialHoldingsNormalizationService

        return OfficialHoldingsNormalizationService(self.store).normalize(
            source_batch_ids
        )

    def build_from_directory(
        self,
        input_dir: Path | str,
        *,
        source_batch_ids: Iterable[str],
        approved_overrides: Iterable[str] = (),
        universe_id: str = UNIVERSE_ID,
    ) -> USPITRelease:
        """Build from an explicitly reviewed normalized Parquet directory.

        No historical membership, identity, action, or bar value is inferred
        here.  Each required table must be supplied and all provenance must
        already have been captured in one or more immutable source batches.
        """

        root = Path(input_dir).resolve()
        if not root.is_dir():
            raise ValueError(f"reviewed PIT input directory not found: {root}")
        market_batch_id, upstream_review_gaps = self._verify_reviewed_build_input(root)
        artifacts: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for dataset in REQUIRED_ARTIFACT_COLUMNS:
            path = root / f"{dataset}.parquet"
            if not path.is_file():
                missing.append(path.name)
                continue
            artifacts[dataset] = pd.read_parquet(path)
        if missing:
            raise ValueError(
                "reviewed PIT input is incomplete; missing: " + ", ".join(missing)
            )
        batch_ids = tuple(dict.fromkeys(str(item).strip() for item in source_batch_ids))
        if not batch_ids:
            raise ValueError("at least one captured --source-batch is required")
        if market_batch_id not in batch_ids:
            raise ValueError(
                "the prepare-market source batch must be supplied via --source-batch: "
                f"{market_batch_id}"
            )
        dependencies: dict[tuple[str, str, str | None, str], SourceDependency] = {}
        for batch_id in batch_ids:
            for item in self.store.load_source_batch(batch_id).dependencies:
                key = (item.source_id, item.dataset, item.as_of_date, item.object_sha256)
                dependencies[key] = item
        return self.build(
            artifacts,
            sources=tuple(
                dependencies[key]
                for key in sorted(
                    dependencies,
                    key=lambda values: tuple(
                        "" if value is None else str(value) for value in values
                    ),
                )
            ),
            universe_id=universe_id,
            metadata={
                "build_input": "reviewed_normalized_parquet",
                "source_batch_ids": list(batch_ids),
                "upstream_review_gaps": upstream_review_gaps,
            },
            approved_overrides=approved_overrides,
        )

    def _verify_reviewed_build_input(
        self, root: Path
    ) -> tuple[str, Mapping[str, Any]]:
        """Require an immutable, gap-free market-prepared workspace."""

        import json

        manifest_path = root / "reviewed_manifest.json"
        report_path = root / "market_prepare_report.json"
        lineage_path = root / "market_source_batch.json"
        if not manifest_path.is_file() or not report_path.is_file() or not lineage_path.is_file():
            raise ValueError(
                "reviewed PIT input must be produced by us-pit prepare-market"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
        if manifest.get("format_version") != "us-pit-reviewed-market-v1":
            raise ValueError("reviewed market manifest format is unsupported")
        if report.get("format_version") != "us-pit-market-prepare-v1":
            raise ValueError("reviewed market report format is unsupported")
        market_status = str(report.get("status") or "")
        market_gaps = report.get("gaps")
        if market_status not in {"MARKET_READY", "DATA_BLOCKED"}:
            raise ValueError("reviewed PIT input has an unsupported market status")
        if not isinstance(market_gaps, list):
            raise ValueError("reviewed PIT input market gaps are malformed")
        if market_status == "MARKET_READY" and market_gaps:
            raise ValueError("MARKET_READY input cannot retain blocking gaps")
        if market_status == "DATA_BLOCKED" and not market_gaps:
            raise ValueError("DATA_BLOCKED input must retain its structured gaps")
        batch_id = str(manifest.get("source_batch_id") or "")
        if lineage.get("batch_id") != batch_id or report.get("source_batch_id") != batch_id:
            raise ValueError("reviewed market source batch lineage disagrees")
        stored_batch = self.store.load_source_batch(batch_id)
        recorded_dependencies = list(lineage.get("dependencies") or [])
        expected_dependencies = [
            item.to_dict() for item in stored_batch.dependencies
        ]
        if recorded_dependencies != expected_dependencies:
            raise ValueError("reviewed market source dependencies disagree with the store")
        files = dict(manifest.get("files") or {})
        required_files = {
            f"{dataset}.parquet" for dataset in REQUIRED_ARTIFACT_COLUMNS
        }
        missing = sorted(required_files - set(files))
        if missing:
            raise ValueError(
                "reviewed market manifest is incomplete; missing: "
                + ", ".join(missing)
            )
        for filename, descriptor_value in files.items():
            descriptor = dict(descriptor_value)
            path = root / str(filename)
            if path.parent.resolve() != root or not path.is_file():
                raise ValueError(f"reviewed market artifact is missing: {filename}")
            if sha256_file(path) != descriptor.get("sha256"):
                raise ValueError(f"reviewed market artifact hash mismatch: {filename}")
        upstream_review_gaps = report.get("upstream_review_gaps")
        if upstream_review_gaps is None:
            upstream_review_gaps = {}
        if not isinstance(upstream_review_gaps, Mapping):
            raise ValueError("reviewed market upstream gap summary is malformed")
        return batch_id, dict(upstream_review_gaps)

    def release_detail(self, release_id: str) -> dict[str, Any]:
        release = self.store.load_release(release_id)
        summary = self._release_summary(release)
        return {
            **summary,
            "format_version": release.manifest.format_version,
            "artifacts": {
                name: descriptor.to_dict()
                for name, descriptor in release.manifest.artifacts.items()
            },
            "sources": [item.to_dict() for item in release.manifest.sources],
            "metadata": dict(release.manifest.metadata),
            "quality_report": release.quality_report.to_dict(),
        }

    def validate_release(self, release_id: str) -> "QualityReport":
        return self.validate(release_id)

    def load_backtest_dataset(self, release_id: str) -> "USBacktestDataset":
        """Load only a hash-verified DATA_READY release for the strict engine."""

        return self.store.load_release(release_id).to_backtest_dataset()

    @staticmethod
    def _release_summary(release: USPITRelease) -> dict[str, Any]:
        report = release.quality_report
        metrics = dict(report.metrics)
        return {
            "release_id": release.release_id,
            "universe_id": release.universe_id,
            "created_at": release.manifest.created_at,
            "status": release.status.value,
            "includes_delisted": report.includes_delisted,
            "quality_policy_version": release.manifest.quality_policy_version,
            "artifact_count": len(release.manifest.artifacts),
            "source_count": len(release.manifest.sources),
            "certified_start": metrics.get("certified_start"),
            "certified_end": metrics.get("certified_end"),
            "metrics": metrics,
        }

    def sync(self, adapter: SourceAdapter, request: SyncRequest) -> SourceBatch:
        if not getattr(adapter, "source_id", "") or not getattr(adapter, "source_version", ""):
            raise ValueError("source adapter must declare source_id and source_version")
        dependencies: list[SourceDependency] = []
        for artifact in adapter.fetch(request):
            if artifact.observed_at > request.observed_at:
                raise ValueError("source artifact observed_at is after the sync observation time")
            if artifact.published_at is not None and artifact.published_at > artifact.observed_at:
                raise ValueError("source artifact was observed before publication")
            reference = self.store.put_bytes(artifact.payload, media_type=artifact.media_type)
            dependencies.append(
                SourceDependency(
                    source_id=adapter.source_id,
                    source_version=adapter.source_version,
                    role=artifact.role,
                    license_class=artifact.license_class,
                    object_sha256=reference.sha256,
                    observed_at=artifact.observed_at.isoformat(),
                    url=artifact.url,
                    dataset=artifact.dataset,
                    as_of_date=None
                    if artifact.as_of_date is None
                    else artifact.as_of_date.isoformat(),
                    published_at=None
                    if artifact.published_at is None
                    else artifact.published_at.isoformat(),
                    metadata=dict(artifact.metadata),
                )
            )
        if not dependencies:
            raise ValueError("source adapter returned no artifacts")
        return self.store.write_source_batch(dependencies)

    def build(
        self,
        artifacts: Mapping[str, pd.DataFrame | ObjectRef],
        *,
        sources: tuple[SourceDependency, ...] | list[SourceDependency],
        universe_id: str = UNIVERSE_ID,
        created_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
        approved_overrides: Iterable[str] = (),
    ) -> USPITRelease:
        if "quality_report" in artifacts:
            raise ValueError("quality_report is derived and cannot be supplied by callers")
        if universe_id != self.validator.policy.universe_id:
            raise ValueError("release universe does not match the quality policy")
        created = created_at or datetime.now(timezone.utc)
        if created.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")

        artifact_values = dict(artifacts)
        override_ids = tuple(sorted(set(approved_overrides)))
        applied_override_records: tuple[dict[str, str], ...] = ()
        if override_ids:
            normalized_frames: dict[str, pd.DataFrame] = {}
            for name, value in artifact_values.items():
                if isinstance(value, pd.DataFrame):
                    normalized_frames[name] = value.copy()
                elif isinstance(value, ObjectRef) and value.media_type == PARQUET_MEDIA_TYPE:
                    normalized_frames[name] = pd.read_parquet(value.path)
            normalized_frames, applied_override_records = self.apply_approved_overrides(
                normalized_frames, override_ids
            )
            artifact_values.update(normalized_frames)

        references: dict[str, ObjectRef] = {}
        frames: dict[str, pd.DataFrame] = {}
        for name, value in artifact_values.items():
            if isinstance(value, pd.DataFrame):
                references[name] = self.store.put_dataframe(value)
                frames[name] = value.copy()
            elif isinstance(value, ObjectRef):
                if not value.path.is_file() or sha256_file(value.path) != value.sha256:
                    raise ValueError(f"release object is missing or corrupt: {name}")
                references[name] = value
                if value.media_type == PARQUET_MEDIA_TYPE:
                    frames[name] = pd.read_parquet(value.path)
            else:
                raise TypeError(f"unsupported release artifact value for {name}: {type(value)!r}")

        source_items = tuple(sources)
        for source in source_items:
            source_path = self.store.object_path(source.object_sha256)
            if not source_path.is_file() or sha256_file(source_path) != source.object_sha256:
                raise ValueError(
                    f"source dependency is not captured in the content store: {source.source_id}/"
                    f"{source.dataset}"
                )
        self._verify_lifecycle_source_payloads(source_items)
        report = self.validator.validate(frames, source_items)
        quality_reference = self.store.put_bytes(
            canonical_json_bytes(report.to_dict()), media_type=JSON_MEDIA_TYPE
        )
        references["quality_report"] = quality_reference
        descriptors = {
            name: self.store.descriptor(name, reference)
            for name, reference in references.items()
        }
        manifest_metadata = dict(metadata or {})
        if applied_override_records:
            manifest_metadata["approved_overrides"] = list(applied_override_records)
        manifest = ReleaseManifest(
            universe_id=universe_id,
            created_at=created.isoformat(),
            status=report.status,
            artifacts=descriptors,
            sources=source_items,
            quality_policy_version=self.validator.policy.version,
            metadata=manifest_metadata,
        )
        return self.store.publish_release(manifest, references)

    def _verify_lifecycle_source_payloads(
        self, sources: tuple[SourceDependency, ...]
    ) -> None:
        """Recheck lifecycle observation identifiers against immutable CAS bytes."""

        indexed = {
            (item.source_id, item.dataset, item.object_sha256): item
            for item in sources
        }
        for summary in (item for item in sources if item.dataset == "lifecycle_status"):
            metadata = dict(summary.metadata)
            if int(metadata.get("coverage_contract_version", 0)) != 3:
                continue
            records = metadata.get("source_records")
            if not isinstance(records, list) or not records:
                raise ValueError("lifecycle v3 source records are missing")
            for record in records:
                if not isinstance(record, Mapping):
                    raise ValueError("lifecycle v3 source record is malformed")
                key = (
                    str(record.get("source_id", "")).strip(),
                    str(record.get("dataset", "")).strip(),
                    str(record.get("evidence_sha256", "")).strip().lower(),
                )
                dependency = indexed.get(key)
                if dependency is None or dependency is summary:
                    raise ValueError("lifecycle v3 source record is not captured")
                payload = self.store.object_path(key[2]).read_bytes().upper()
                compact = re.sub(rb"[^A-Z0-9]", b"", payload)
                normalized_payload = re.sub(rb"\s+", b" ", payload)
                observations = record.get("observations")
                if not isinstance(observations, list) or not observations:
                    raise ValueError("lifecycle v3 observations are missing")
                for observation in observations:
                    if not isinstance(observation, Mapping):
                        raise ValueError("lifecycle v3 observation is malformed")
                    identifier = re.sub(
                        r"[^A-Z0-9]", "",
                        str(observation.get("identifier_value", "")).upper(),
                    )
                    if not identifier or identifier.encode("ascii") not in compact:
                        raise ValueError(
                            "lifecycle v3 observation identifier is absent from its "
                            "captured source payload"
                        )
                    excerpt = re.sub(
                        rb"\s+", b" ",
                        str(observation.get("evidence_excerpt", ""))
                        .upper().encode("utf-8"),
                    )
                    if not excerpt or excerpt not in normalized_payload:
                        raise ValueError(
                            "lifecycle v3 observation excerpt is absent from its "
                            "captured source payload"
                        )
                    source_day = pd.Timestamp(
                        dependency.as_of_date or dependency.published_at
                    ).date()
                    observed_through = pd.Timestamp(
                        observation.get("observed_through")
                    ).date()
                    if observed_through > source_day:
                        raise ValueError(
                            "lifecycle v3 observation claims coverage after its source"
                        )

    def apply_approved_overrides(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        override_ids: Iterable[str],
    ) -> tuple[dict[str, pd.DataFrame], tuple[dict[str, str], ...]]:
        """Apply current approved drafts to normalized copies, never raw evidence."""

        result = {name: frame.copy() for name, frame in artifacts.items()}
        applied: list[dict[str, str]] = []
        for override_id in sorted(set(override_ids)):
            state = self.store.get_override(override_id)
            if not state.approved:
                raise ValueError(f"override is not approved for its current draft: {override_id}")
            proposal = state.proposal
            frame = result.get(proposal.dataset)
            if frame is None:
                raise ValueError(
                    f"override target dataset is absent: {override_id}/{proposal.dataset}"
                )
            missing_keys = sorted(set(proposal.record_key) - set(frame.columns))
            if missing_keys:
                raise ValueError(
                    f"override key columns are absent for {override_id}: {missing_keys}"
                )
            mask = pd.Series(True, index=frame.index, dtype=bool)
            for column, expected in proposal.record_key.items():
                if expected is None:
                    mask &= frame[column].isna()
                else:
                    mask &= frame[column].astype(str).eq(str(expected))
            matches = frame.index[mask]
            if proposal.before is None:
                if len(matches):
                    raise ValueError(
                        f"additive override target already exists: {override_id}"
                    )
                row = {**dict(proposal.record_key), **dict(proposal.after)}
                result[proposal.dataset] = pd.concat(
                    [frame, pd.DataFrame([row])], ignore_index=True
                )
            else:
                if len(matches) != 1:
                    raise ValueError(
                        f"override expected exactly one target row for {override_id}, "
                        f"found {len(matches)}"
                    )
                index = matches[0]
                for column, expected in proposal.before.items():
                    if column not in frame.columns:
                        raise ValueError(
                            f"override before-column is absent for {override_id}: {column}"
                        )
                    actual = frame.at[index, column]
                    equal = (
                        pd.isna(actual) and expected is None
                    ) or str(actual) == str(expected)
                    if not equal:
                        raise ValueError(
                            f"override before-value changed for {override_id}/{column}"
                        )
                for column, value in proposal.after.items():
                    if column not in frame.columns:
                        frame[column] = None
                    frame.at[index, column] = value
                result[proposal.dataset] = frame
            applied.append(
                {
                    "override_id": override_id,
                    "draft_sha256": state.draft_sha256,
                }
            )
        return result, tuple(applied)

    def validate(self, release_id: str) -> "QualityReport":
        from .models import QualityReport

        release = self.store.load_release(release_id)
        frames: dict[str, pd.DataFrame] = {}
        for name, descriptor in release.manifest.artifacts.items():
            if name != "quality_report" and descriptor.media_type == PARQUET_MEDIA_TYPE:
                frames[name] = release.load_frame(name)
        recomputed = self.validator.validate(frames, release.manifest.sources)
        stored = release.quality_report
        if recomputed.to_dict() != stored.to_dict():
            raise ValueError(
                "stored quality report does not match deterministic validation; "
                "build a new immutable release"
            )
        return recomputed


__all__ = ["USPITService"]
