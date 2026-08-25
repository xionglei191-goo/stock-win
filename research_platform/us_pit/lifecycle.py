from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .hashing import sha256_file, sha256_json
from .models import LicenseClass, SourceDependency, SourceRole
from .sources_reviewed import ReviewedEvidenceSpec, ReviewedLocalEvidenceAdapter
from .store import USPITStore


LIFECYCLE_FORMAT_VERSION = "us-lifecycle-surveillance-v3"
LIFECYCLE_COVERAGE_CONTRACT_VERSION = 3
_ALLOWED_RECORD_DATASETS = frozenset(
    {
        "fund_holdings_observed",
        "membership_events",
        "corporate_actions",
        "session_exceptions",
        "lifecycle_observation",
    }
)


@dataclass(frozen=True)
class LifecycleSourceRecord:
    source_id: str
    dataset: str
    evidence_sha256: str
    published_at: str
    url: str
    observations: tuple[dict[str, str], ...]

    @property
    def covered_security_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item["security_id"] for item in self.observations}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "dataset": self.dataset,
            "evidence_sha256": self.evidence_sha256,
            "published_at": self.published_at,
            "url": self.url,
            "observations": [dict(item) for item in self.observations],
        }


@dataclass(frozen=True)
class LifecycleSurveillanceDocument:
    current_through: date
    covered_security_ids: tuple[str, ...]
    source_records: tuple[LifecycleSourceRecord, ...]

    @property
    def covered_security_ids_sha256(self) -> str:
        return sha256_json(list(self.covered_security_ids))

    @property
    def source_records_sha256(self) -> str:
        return sha256_json([item.to_dict() for item in self.source_records])


def _stable_ids(values: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise ValueError(f"lifecycle {field} requires a non-empty list")
    normalized = tuple(sorted(str(item).strip().lower() for item in values))
    if (
        len(normalized) != len(set(normalized))
        or any(not item.startswith("us_") for item in normalized)
    ):
        raise ValueError(
            f"lifecycle {field} must contain unique stable us_* security IDs"
        )
    return normalized


def _observations(values: object, *, field: str) -> tuple[dict[str, str], ...]:
    if not isinstance(values, list) or not values:
        raise ValueError(f"lifecycle {field} requires non-empty observations")
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ordinal, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise ValueError(f"lifecycle {field}[{ordinal}] must be an object")
        security_id = str(raw.get("security_id", "")).strip().lower()
        identifier_type = str(raw.get("identifier_type", "")).strip().upper()
        identifier_value = re.sub(
            r"[^A-Z0-9]", "", str(raw.get("identifier_value", "")).upper()
        )
        observed_status = str(raw.get("observed_status", "")).strip().upper()
        evidence_locator = str(raw.get("evidence_locator", "")).strip()
        observed_through = str(raw.get("observed_through", "")).strip()
        status_effective_at = str(raw.get("status_effective_at", "")).strip()
        evidence_excerpt = str(raw.get("evidence_excerpt", "")).strip()
        try:
            date.fromisoformat(observed_through)
            if status_effective_at:
                date.fromisoformat(status_effective_at)
        except ValueError as exc:
            raise ValueError(
                f"lifecycle {field}[{ordinal}] has an invalid evidence date"
            ) from exc
        identity = (security_id, identifier_type, identifier_value)
        if (
            not security_id.startswith("us_")
            or identifier_type not in {"ISIN", "CUSIP"}
            or not identifier_value
            or observed_status not in {
                "LISTED", "ACTIVE_HOLDING", "TERMINATED", "DELISTED",
                "MERGED", "BANKRUPT", "HALTED",
            }
            or not evidence_locator
            or not observed_through
            or not evidence_excerpt
            or len(evidence_excerpt) > 500
            or observed_status in {"TERMINATED", "DELISTED", "MERGED", "BANKRUPT"}
            and not status_effective_at
            or identity in seen
        ):
            raise ValueError(f"lifecycle {field}[{ordinal}] is invalid")
        seen.add(identity)
        output.append(
            {
                "security_id": security_id,
                "identifier_type": identifier_type,
                "identifier_value": identifier_value,
                "observed_status": observed_status,
                "evidence_locator": evidence_locator,
                "observed_through": observed_through,
                "status_effective_at": status_effective_at,
                "evidence_excerpt": evidence_excerpt,
            }
        )
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item["security_id"], item["identifier_type"], item["identifier_value"]
            ),
        )
    )


def load_lifecycle_surveillance(path: Path | str) -> LifecycleSurveillanceDocument:
    source = Path(path).expanduser().resolve(strict=True)
    value = json.loads(source.read_text(encoding="utf-8"))
    if value.get("format_version") != LIFECYCLE_FORMAT_VERSION:
        raise ValueError(
            "unsupported lifecycle surveillance format; v2 evidence bindings are required"
        )
    current_through = date.fromisoformat(str(value.get("current_through", "")))
    raw_records = value.get("source_records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("lifecycle surveillance requires official source_records")
    records: list[LifecycleSourceRecord] = []
    covered_union: set[str] = set()
    identities: set[tuple[str, str, str]] = set()
    for ordinal, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            raise ValueError("lifecycle source_records must be objects")
        source_id = str(raw.get("source_id", "")).strip()
        dataset = str(raw.get("dataset", "")).strip()
        digest = str(raw.get("evidence_sha256", "")).strip().lower()
        published_at = str(raw.get("published_at", "")).strip()
        url = str(raw.get("url", "")).strip()
        if not source_id or dataset not in _ALLOWED_RECORD_DATASETS:
            raise ValueError(f"lifecycle source record {ordinal} has invalid source identity")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"lifecycle source record {ordinal} has invalid SHA-256")
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if published.tzinfo is None or not url.startswith("https://"):
            raise ValueError(
                f"lifecycle source record {ordinal} lacks causal publication time or HTTPS URL"
            )
        observations = _observations(
            raw.get("observations"),
            field=f"source_records[{ordinal}].observations",
        )
        ids = tuple(sorted({item["security_id"] for item in observations}))
        identity = (source_id, dataset, digest)
        if identity in identities:
            raise ValueError("lifecycle source record identity is duplicated")
        identities.add(identity)
        covered_union.update(ids)
        records.append(
            LifecycleSourceRecord(
                source_id=source_id,
                dataset=dataset,
                evidence_sha256=digest,
                published_at=published.isoformat(),
                url=url,
                observations=observations,
            )
        )
    records.sort(key=lambda item: (item.source_id, item.dataset, item.evidence_sha256))
    derived_ids = tuple(sorted(covered_union))
    asserted_ids = _stable_ids(
        value.get("covered_security_ids"), field="covered_security_ids"
    )
    if asserted_ids != derived_ids:
        raise ValueError(
            "lifecycle covered_security_ids must equal the union derived from source_records"
        )
    asserted_hash = str(value.get("covered_security_ids_sha256", "")).lower()
    if asserted_hash != sha256_json(list(derived_ids)):
        raise ValueError("lifecycle covered_security_ids_sha256 mismatch")
    asserted_records_hash = str(value.get("source_records_sha256", "")).lower()
    calculated_records_hash = sha256_json([item.to_dict() for item in records])
    if asserted_records_hash != calculated_records_hash:
        raise ValueError("lifecycle source_records_sha256 mismatch")
    return LifecycleSurveillanceDocument(
        current_through=current_through,
        covered_security_ids=derived_ids,
        source_records=tuple(records),
    )


def _bind_source_records(
    document: LifecycleSurveillanceDocument,
    *,
    store: USPITStore,
    source_batch_ids: Iterable[str],
) -> tuple[SourceDependency, ...]:
    batches = tuple(sorted(set(str(item).strip() for item in source_batch_ids)))
    if not batches or any(not item for item in batches):
        raise ValueError("lifecycle v2 import requires at least one source batch")
    dependencies: dict[tuple[str, str, str], list[SourceDependency]] = {}
    for batch_id in batches:
        for dependency in store.load_source_batch(batch_id).dependencies:
            key = (
                dependency.source_id,
                dependency.dataset,
                dependency.object_sha256,
            )
            dependencies.setdefault(key, []).append(dependency)
    bound: list[SourceDependency] = []
    for record in document.source_records:
        key = (record.source_id, record.dataset, record.evidence_sha256)
        candidates = dependencies.get(key, [])
        exact = [
            item
            for item in candidates
            if item.url == record.url
            and item.published_at is not None
            and datetime.fromisoformat(item.published_at.replace("Z", "+00:00"))
            == datetime.fromisoformat(record.published_at)
            and item.license_class != LicenseClass.UNLICENSED_REFERENCE
            and item.role in {SourceRole.SIGNAL_INPUT, SourceRole.VALIDATION_ANCHOR}
        ]
        if len(exact) != 1:
            raise ValueError(
                "lifecycle source record does not resolve to exactly one captured "
                f"official dependency: {record.source_id}/{record.dataset}/"
                f"{record.evidence_sha256}"
            )
        object_path = store.object_path(record.evidence_sha256)
        if not object_path.is_file() or sha256_file(object_path) != record.evidence_sha256:
            raise ValueError("lifecycle source record CAS object is missing or corrupt")
        payload = object_path.read_bytes().upper()
        normalized_payload = re.sub(rb"\s+", b" ", payload)
        for observation in record.observations:
            identifier = observation["identifier_value"].encode("ascii")
            compact_payload = re.sub(rb"[^A-Z0-9]", b"", payload)
            if identifier not in compact_payload:
                raise ValueError(
                    "lifecycle observation identifier is absent from its frozen "
                    f"evidence: {observation['security_id']}/"
                    f"{observation['identifier_type']}/{observation['identifier_value']}"
                )
            excerpt = re.sub(
                rb"\s+", b" ", observation["evidence_excerpt"].upper().encode("utf-8")
            )
            if excerpt not in normalized_payload:
                raise ValueError(
                    "lifecycle observation excerpt is absent from its frozen evidence: "
                    f"{observation['security_id']}"
                )
            source = exact[0]
            evidence_day = date.fromisoformat(
                source.as_of_date
                or datetime.fromisoformat(
                    source.published_at.replace("Z", "+00:00")
                ).date().isoformat()
            )
            observed_through = date.fromisoformat(observation["observed_through"])
            if observed_through > evidence_day:
                raise ValueError(
                    "lifecycle observation claims coverage after its source evidence"
                )
        bound.append(exact[0])
    return tuple(bound)


def lifecycle_evidence_adapter(
    *,
    path: Path | str,
    source_id: str,
    source_version: str,
    public_url: str,
    published_at: datetime,
    store: USPITStore,
    source_batch_ids: Iterable[str],
) -> ReviewedLocalEvidenceAdapter:
    document = load_lifecycle_surveillance(path)
    bound = _bind_source_records(
        document, store=store, source_batch_ids=source_batch_ids
    )
    record_values = [item.to_dict() for item in document.source_records]
    return ReviewedLocalEvidenceAdapter(
        ReviewedEvidenceSpec(
            path=Path(path),
            dataset="lifecycle_status",
            source_id=source_id,
            source_version=source_version,
            public_url=public_url,
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            published_at=published_at,
            as_of_date=document.current_through,
            media_type="application/json",
            authority="VERIFIED_MULTI_SOURCE_LIFECYCLE",
            metadata={
                "coverage_contract_version": LIFECYCLE_COVERAGE_CONTRACT_VERSION,
                "coverage_kind": "TERMINATION_SURVEILLANCE",
                "current_through": document.current_through.isoformat(),
                "covered_security_ids": list(document.covered_security_ids),
                "covered_security_ids_sha256": document.covered_security_ids_sha256,
                "covered_security_count": len(document.covered_security_ids),
                "source_records": record_values,
                "source_records_sha256": document.source_records_sha256,
                "source_record_count": len(record_values),
                "source_dependency_object_sha256s": sorted(
                    item.object_sha256 for item in bound
                ),
                "coverage_derived_from_payload": True,
                "source_records_bound_to_cas": True,
                "observation_identifiers_verified_in_payload": True,
            },
        )
    )


__all__ = [
    "LIFECYCLE_COVERAGE_CONTRACT_VERSION",
    "LIFECYCLE_FORMAT_VERSION",
    "LifecycleSourceRecord",
    "LifecycleSurveillanceDocument",
    "lifecycle_evidence_adapter",
    "load_lifecycle_surveillance",
]
