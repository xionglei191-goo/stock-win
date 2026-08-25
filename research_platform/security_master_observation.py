from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from . import bse_current_delisting_events as bse_current
from . import pending_listing_source as pending_listing


PROTOCOL_VERSION = "cn-security-master-current-observation-v3"
OBSERVATION_READY = "CURRENT_MASTER_OBSERVATION_READY"
OBSERVATION_REJECTED = "CURRENT_MASTER_OBSERVATION_REJECTED"

MAX_OBSERVATION_WINDOW = timedelta(minutes=5)
MAX_LIVE_CLOCK_SKEW = timedelta(seconds=5)
MAX_TDX_OBSERVATION_AGE = timedelta(seconds=30)
MAX_MANIFEST_BYTES = 2 * 1024 * 1024

TDX_STOCK_LIST_METHOD = "get_stock_list"
TDX_STOCK_LIST_PARAMS = {"market": "5", "list_type": 1}

PENDING_REQUIRED_CODES = frozenset(
    {
        "301655.SZ",
        "301688.SZ",
        "301697.SZ",
        "688826.SH",
        "688835.SH",
        "688836.SH",
    }
)
BSE_TERMINATED_CODES = frozenset({"920305.BJ", "920680.BJ"})

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PENDING_CAS_ROOT = (
    _REPOSITORY_ROOT / "data" / "security_master" / "pending_listing" / "cas"
)
DEFAULT_BSE_CURRENT_DELISTING_CAS_ROOT = (
    _REPOSITORY_ROOT
    / "data"
    / "security_master"
    / "bse_current_delisting"
    / "cas"
)


class SecurityMasterObservationBlockedError(RuntimeError):
    """The current master evidence cannot form one trustworthy observation."""

    def __init__(self, message: str, *, status: str = OBSERVATION_REJECTED) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SecurityMasterObservationPolicy:
    pending_cas_root: Path = DEFAULT_PENDING_CAS_ROOT
    bse_current_delisting_cas_root: Path = DEFAULT_BSE_CURRENT_DELISTING_CAS_ROOT
    minimum_tdx_code_count: int = 5_000

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_tdx_code_count, bool)
            or not isinstance(self.minimum_tdx_code_count, int)
            or self.minimum_tdx_code_count < len(PENDING_REQUIRED_CODES)
        ):
            raise ValueError("minimum_tdx_code_count is below the admitted bound")


@dataclass(frozen=True)
class UnderlyingManifestReference:
    cas_root: Path
    manifest_sha256: str
    logical_content_sha256: str
    claimed_earliest_retrieved_at: str | None = None
    claimed_latest_retrieved_at: str | None = None


@dataclass(frozen=True)
class TDXAShareObservation:
    observed_at: str
    codes: tuple[str, ...]
    names: Mapping[str, str]
    code_count: int
    code_set_sha256: str
    identity_sha256: str

    @classmethod
    def capture(
        cls,
        rows_or_mapping: Any,
        *,
        observed_at: datetime | str,
    ) -> "TDXAShareObservation":
        identities = _extract_tdx_a_share_identities(rows_or_mapping)
        codes = tuple(code for code, _name in identities)
        names = _freeze_tdx_names(identities)
        return cls(
            observed_at=_normalize_timestamp(observed_at, "TDX observed_at"),
            codes=codes,
            names=names,
            code_count=len(codes),
            code_set_sha256=_tdx_code_set_sha256(codes),
            identity_sha256=_tdx_identity_sha256(identities),
        )

    def to_dict(self) -> dict[str, Any]:
        codes, identities, code_digest, identity_digest = (
            _validate_tdx_identity_components(self.codes, self.names)
        )
        if (
            self.code_count != len(codes)
            or self.code_set_sha256 != code_digest
            or self.identity_sha256 != identity_digest
        ):
            raise SecurityMasterObservationBlockedError(
                "TDX caller count/hash summaries do not match the exact identity map"
            )
        return {
            "observed_at": self.observed_at,
            "codes": list(codes),
            "names": {code: name for code, name in identities},
            "code_count": len(codes),
            "code_set_sha256": code_digest,
            "identity_sha256": identity_digest,
        }


@dataclass(frozen=True)
class ObservationSourceEvidence:
    source_name: str
    cas_root: str
    protocol_version: str
    manifest_sha256: str
    logical_content_sha256: str
    earliest_retrieved_at: str
    latest_retrieved_at: str
    raw_capture_count: int
    target_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["target_codes"] = list(self.target_codes)
        return value


@dataclass(frozen=True)
class SecurityMasterObservationBatch:
    as_of: str
    validated_at: str
    pending_listing: ObservationSourceEvidence
    bse_current_delisting: ObservationSourceEvidence
    tdx_a_share: TDXAShareObservation
    observation_earliest_at: str
    observation_latest_at: str
    observation_span_seconds: int
    logical_content_sha256: str

    @property
    def status(self) -> str:
        return OBSERVATION_READY

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "status": self.status,
            "as_of": self.as_of,
            "validated_at": self.validated_at,
            "pending_listing": self.pending_listing.to_dict(),
            "bse_current_delisting": self.bse_current_delisting.to_dict(),
            "tdx_a_share": self.tdx_a_share.to_dict(),
            "observation_earliest_at": self.observation_earliest_at,
            "observation_latest_at": self.observation_latest_at,
            "observation_span_seconds": self.observation_span_seconds,
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": _source_contract(),
        }


@dataclass(frozen=True)
class ObservationManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assemble_security_master_observation(
    *,
    policy: SecurityMasterObservationPolicy,
    pending_manifest: UnderlyingManifestReference,
    bse_current_delisting_manifest: UnderlyingManifestReference,
    tdx_observation: TDXAShareObservation,
    as_of: datetime | str,
) -> SecurityMasterObservationBatch:
    """Cold-replay three inputs and bind them to one short current-time window."""

    return _assemble_security_master_observation(
        policy=policy,
        pending_manifest=pending_manifest,
        bse_current_delisting_manifest=bse_current_delisting_manifest,
        tdx_observation=tdx_observation,
        as_of=as_of,
        validation_now=_wall_clock(),
    )


def capture_current_security_master_observation(
    runtime_dir: Path,
    *,
    policy: SecurityMasterObservationPolicy | None = None,
    pending_manifest_capture: Callable[[], UnderlyingManifestReference]
    | None = None,
    bse_manifest_capture: Callable[[], UnderlyingManifestReference] | None = None,
    tdx_stock_list_loader: Callable[[], Any] | None = None,
    tdx_code_loader: Callable[[], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    tdx_timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Capture and seal one read-only current-master observation.

    The production path is deliberately narrow: two official current-evidence
    collectors plus one TDX ``get_stock_list`` call.  The returned artifact is
    audit evidence only; it cannot publish a security master, train a model, or
    reach a broker surface.
    """

    if tdx_timeout_seconds <= 0:
        raise ValueError("tdx_timeout_seconds must be positive")
    if tdx_stock_list_loader is not None and tdx_code_loader is not None:
        raise ValueError(
            "provide only one TDX stock-list capture injection"
        )
    now = clock or _wall_clock
    base = _lexical_absolute_path(Path(runtime_dir)) / "security_master"
    active_policy = policy or SecurityMasterObservationPolicy(
        pending_cas_root=base / "pending_listing" / "cas",
        bse_current_delisting_cas_root=base / "bse_current_delisting" / "cas",
    )

    pending_capture = pending_manifest_capture or (
        lambda: _capture_pending_listing_manifest(
            active_policy.pending_cas_root,
            clock=now,
        )
    )
    bse_capture = bse_manifest_capture or (
        lambda: _capture_bse_current_delisting_manifest(
            active_policy.bse_current_delisting_cas_root,
            clock=now,
        )
    )
    # ``tdx_code_loader`` remains as a compatibility keyword only.  Its return
    # contract is v3: exact raw Code/Name rows or a code-to-name mapping.  A
    # legacy sequence of codes reaches the same strict parser and is rejected.
    stock_list_loader = tdx_stock_list_loader or tdx_code_loader or (
        lambda: _load_current_tdx_stock_list(
            timeout_seconds=tdx_timeout_seconds
        )
    )

    pending_reference = pending_capture()
    if not isinstance(pending_reference, UnderlyingManifestReference):
        raise TypeError(
            "pending_manifest_capture must return UnderlyingManifestReference"
        )
    bse_reference = bse_capture()
    if not isinstance(bse_reference, UnderlyingManifestReference):
        raise TypeError(
            "bse_manifest_capture must return UnderlyingManifestReference"
        )

    stock_list = stock_list_loader()
    tdx_observed_at = now()
    tdx_observation = TDXAShareObservation.capture(
        stock_list,
        observed_at=tdx_observed_at,
    )
    as_of = now()
    batch = _assemble_security_master_observation(
        policy=active_policy,
        pending_manifest=pending_reference,
        bse_current_delisting_manifest=bse_reference,
        tdx_observation=tdx_observation,
        as_of=as_of,
        validation_now=now(),
    )
    store = SecurityMasterObservationStore(
        base / "current_observations",
        policy=active_policy,
    )
    manifest_reference = store.seal(batch)
    replayed = store.replay(manifest_reference.manifest_sha256)
    if replayed.to_manifest_dict() != batch.to_manifest_dict():
        raise SecurityMasterObservationBlockedError(
            "sealed observation failed immutable audit replay"
        )

    return {
        "status": replayed.status,
        "protocol_version": PROTOCOL_VERSION,
        "manifest": manifest_reference.to_dict(),
        "as_of": replayed.as_of,
        "validated_at": replayed.validated_at,
        "observation_earliest_at": replayed.observation_earliest_at,
        "observation_latest_at": replayed.observation_latest_at,
        "observation_span_seconds": replayed.observation_span_seconds,
        "pending_listing_manifest_sha256": (
            replayed.pending_listing.manifest_sha256
        ),
        "bse_current_delisting_manifest_sha256": (
            replayed.bse_current_delisting.manifest_sha256
        ),
        "tdx": {
            "endpoint": "http://127.0.0.1:17709/",
            "method": TDX_STOCK_LIST_METHOD,
            "params": dict(TDX_STOCK_LIST_PARAMS),
            "observed_at": replayed.tdx_a_share.observed_at,
            "code_count": replayed.tdx_a_share.code_count,
            "code_set_sha256": replayed.tdx_a_share.code_set_sha256,
            "identity_sha256": replayed.tdx_a_share.identity_sha256,
        },
        "replay_mode": "IMMUTABLE_AUDIT",
        "audit_only": True,
        "no_master_publish": True,
        "no_training": True,
        "no_trading": True,
    }


def _capture_pending_listing_manifest(
    cas_root: Path,
    *,
    clock: Callable[[], datetime],
) -> UnderlyingManifestReference:
    cas = pending_listing.PendingListingRawCAS(cas_root)
    artifact = pending_listing.PendingListingSourceClient(
        cas=cas,
        clock=clock,
    ).fetch_current()
    reference = pending_listing.PendingListingManifestStore(cas).seal(artifact)
    return UnderlyingManifestReference(
        cas_root=cas.root,
        manifest_sha256=reference.manifest_sha256,
        logical_content_sha256=artifact.logical_content_sha256,
    )


def _capture_bse_current_delisting_manifest(
    cas_root: Path,
    *,
    clock: Callable[[], datetime],
) -> UnderlyingManifestReference:
    cas = bse_current.BSECurrentDelistingCAS(cas_root)
    artifact = bse_current.BSECurrentDelistingClient(
        cas=cas,
        clock=clock,
    ).fetch_current()
    reference = bse_current.BSECurrentDelistingManifestStore(cas).seal(artifact)
    return UnderlyingManifestReference(
        cas_root=cas.root,
        manifest_sha256=reference.manifest_sha256,
        logical_content_sha256=artifact.logical_content_sha256,
    )


def _load_current_tdx_stock_list(
    *,
    timeout_seconds: float,
    client_factory: Callable[[float], Any] | None = None,
) -> Any:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    factory = client_factory or _new_tdx_http_client
    client = factory(float(timeout_seconds))
    return client.call(TDX_STOCK_LIST_METHOD, dict(TDX_STOCK_LIST_PARAMS))


def _new_tdx_http_client(timeout_seconds: float) -> Any:
    from .early_winner_research import TdxResearchHttpClient

    return TdxResearchHttpClient(
        timeout_seconds=timeout_seconds,
        max_attempts=1,
    )


def _extract_tdx_a_share_identities(
    value: Any,
) -> tuple[tuple[str, str], ...]:
    """Canonicalize one stock-list response without collapsing duplicate rows."""

    pairs: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        if not value:
            raise SecurityMasterObservationBlockedError(
                "TDX A-share identity mapping is empty"
            )
        for raw_code, raw_name in value.items():
            code = _validate_tdx_code(raw_code)
            name = _validate_tdx_name(raw_name, code=code)
            pairs.append((code, name))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            raise SecurityMasterObservationBlockedError(
                "TDX get_stock_list returned no identity rows"
            )
        seen: dict[str, str] = {}
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise SecurityMasterObservationBlockedError(
                    "TDX stock-list capture must contain Code/Name rows; "
                    "code-only input is rejected"
                )
            code_keys = [key for key in ("Code", "code") if key in item]
            name_keys = [key for key in ("Name", "name") if key in item]
            if len(code_keys) != 1 or len(name_keys) != 1:
                raise SecurityMasterObservationBlockedError(
                    f"TDX stock-list row {index} lacks one unambiguous Code/Name pair"
                )
            code = _validate_tdx_code(item[code_keys[0]])
            name = _validate_tdx_name(item[name_keys[0]], code=code)
            if code in seen:
                detail = "conflicting " if seen[code] != name else ""
                raise SecurityMasterObservationBlockedError(
                    f"TDX stock-list contains a {detail}duplicate code: {code}"
                )
            seen[code] = name
            pairs.append((code, name))
    else:
        raise SecurityMasterObservationBlockedError(
            "TDX stock-list capture must be raw Code/Name rows or a code-to-name mapping"
        )
    canonical = tuple(sorted(pairs))
    _validate_tdx_names(canonical)
    return canonical


def _assemble_security_master_observation(
    *,
    policy: SecurityMasterObservationPolicy,
    pending_manifest: UnderlyingManifestReference,
    bse_current_delisting_manifest: UnderlyingManifestReference,
    tdx_observation: TDXAShareObservation,
    as_of: datetime | str,
    validation_now: datetime | str,
) -> SecurityMasterObservationBatch:

    if not isinstance(policy, SecurityMasterObservationPolicy):
        raise TypeError("policy must be SecurityMasterObservationPolicy")
    current_text = _normalize_timestamp(
        validation_now,
        "validation wall clock",
    )
    current = datetime.fromisoformat(current_text)
    as_of_text = _normalize_timestamp(as_of, "as_of")
    decision = datetime.fromisoformat(as_of_text)
    if abs(decision - current) > MAX_LIVE_CLOCK_SKEW:
        raise SecurityMasterObservationBlockedError(
            "as_of is not near the actual validation wall clock"
        )

    pending_root = _require_fixed_root(
        pending_manifest.cas_root,
        policy.pending_cas_root,
        "pending-listing",
    )
    bse_root = _require_fixed_root(
        bse_current_delisting_manifest.cas_root,
        policy.bse_current_delisting_cas_root,
        "BSE current-delisting",
    )
    pending_digest = _valid_digest(
        pending_manifest.manifest_sha256, "pending-listing manifest"
    )
    bse_digest = _valid_digest(
        bse_current_delisting_manifest.manifest_sha256,
        "BSE current-delisting manifest",
    )

    try:
        pending_artifact = pending_listing.PendingListingManifestStore(
            pending_listing.PendingListingRawCAS(pending_root)
        ).replay(pending_digest)
        pending_listing.validate_pending_listing_freshness(
            pending_artifact,
            now=current,
            as_of=decision,
        )
    except (pending_listing.PendingListingSourceBlockedError, OSError) as exc:
        raise SecurityMasterObservationBlockedError(
            f"pending-listing manifest failed cold replay: {exc}"
        ) from exc
    if (
        _valid_digest(
            pending_manifest.logical_content_sha256,
            "pending-listing logical content",
        )
        != pending_artifact.logical_content_sha256
    ):
        raise SecurityMasterObservationBlockedError(
            "pending-listing caller logical hash does not match cold replay"
        )
    pending_codes = tuple(sorted(item.code for item in pending_artifact.securities))
    if set(pending_codes) != set(PENDING_REQUIRED_CODES):
        raise SecurityMasterObservationBlockedError(
            "pending-listing manifest does not contain the six frozen target codes"
        )
    pending_times = tuple(
        _parse_timestamp(item.retrieved_at, "pending-listing source time")
        for item in pending_artifact.raw_sources
    )
    pending_evidence = _source_evidence(
        source_name="pending_listing",
        root=pending_root,
        protocol_version=pending_listing.PROTOCOL_VERSION,
        manifest_sha256=pending_digest,
        logical_content_sha256=pending_artifact.logical_content_sha256,
        times=pending_times,
        target_codes=pending_codes,
        reference=pending_manifest,
    )

    try:
        bse_artifact = bse_current.BSECurrentDelistingManifestStore(
            bse_current.BSECurrentDelistingCAS(bse_root)
        ).replay(bse_digest)
        bse_current.validate_current_delisting_freshness(
            bse_artifact,
            now=current,
            as_of=decision,
        )
    except (bse_current.BSECurrentDelistingBlockedError, OSError) as exc:
        raise SecurityMasterObservationBlockedError(
            f"BSE current-delisting manifest failed cold replay: {exc}"
        ) from exc
    if (
        _valid_digest(
            bse_current_delisting_manifest.logical_content_sha256,
            "BSE current-delisting logical content",
        )
        != bse_artifact.logical_content_sha256
    ):
        raise SecurityMasterObservationBlockedError(
            "BSE caller logical hash does not match cold replay"
        )
    bse_codes = tuple(sorted(item.code_alias for item in bse_artifact.events))
    if set(bse_codes) != set(BSE_TERMINATED_CODES):
        raise SecurityMasterObservationBlockedError(
            "BSE current-delisting manifest does not contain both frozen events"
        )
    absent = bse_artifact.completeness.get(
        "target_codes_absent_from_current_catalogue"
    )
    if absent != list(bse_codes):
        raise SecurityMasterObservationBlockedError(
            "BSE current catalogue does not prove both terminated codes absent"
        )
    bse_times = _bse_capture_times(bse_artifact)
    bse_evidence = _source_evidence(
        source_name="bse_current_delisting",
        root=bse_root,
        protocol_version=bse_current.PROTOCOL_VERSION,
        manifest_sha256=bse_digest,
        logical_content_sha256=bse_artifact.logical_content_sha256,
        times=bse_times,
        target_codes=bse_codes,
        reference=bse_current_delisting_manifest,
    )

    tdx = _validate_tdx_observation(
        tdx_observation,
        current=current,
        minimum_count=policy.minimum_tdx_code_count,
    )
    tdx_codes = set(tdx.codes)
    missing_pending = sorted(PENDING_REQUIRED_CODES - tdx_codes)
    if missing_pending:
        raise SecurityMasterObservationBlockedError(
            f"TDX snapshot is missing pending assigned codes: {missing_pending}"
        )
    present_terminated = sorted(BSE_TERMINATED_CODES & tdx_codes)
    if present_terminated:
        raise SecurityMasterObservationBlockedError(
            f"TDX snapshot still contains terminated BSE codes: {present_terminated}"
        )

    all_times = [
        *pending_times,
        *bse_times,
        _parse_timestamp(tdx.observed_at, "TDX observed_at"),
        decision,
        current,
    ]
    earliest = min(all_times)
    latest = max(all_times)
    span = latest - earliest
    if span > MAX_OBSERVATION_WINDOW:
        raise SecurityMasterObservationBlockedError(
            "official evidence, TDX, as_of, and validation clock do not fit one "
            "five-minute observation window"
        )
    if latest > current + MAX_LIVE_CLOCK_SKEW:
        raise SecurityMasterObservationBlockedError(
            "observation batch contains a future-dated timestamp"
        )

    logical_value = {
        "as_of": as_of_text,
        "validated_at": current_text,
        "pending_listing": pending_evidence.to_dict(),
        "bse_current_delisting": bse_evidence.to_dict(),
        "tdx_a_share": tdx.to_dict(),
        "observation_earliest_at": earliest.isoformat(),
        "observation_latest_at": latest.isoformat(),
        "observation_span_seconds": int(span.total_seconds()),
    }
    return SecurityMasterObservationBatch(
        as_of=as_of_text,
        validated_at=current_text,
        pending_listing=pending_evidence,
        bse_current_delisting=bse_evidence,
        tdx_a_share=tdx,
        observation_earliest_at=earliest.isoformat(),
        observation_latest_at=latest.isoformat(),
        observation_span_seconds=int(span.total_seconds()),
        logical_content_sha256=_sha256(_canonical_json_bytes(logical_value)),
    )


class SecurityMasterObservationStore:
    """Content-addressed store whose replay revalidates both upstream CAS trees."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        policy: SecurityMasterObservationPolicy,
    ) -> None:
        if not isinstance(policy, SecurityMasterObservationPolicy):
            raise TypeError("policy must be SecurityMasterObservationPolicy")
        self.root = _lexical_absolute_path(runtime_root)
        self.policy = policy

    def seal(
        self,
        batch: SecurityMasterObservationBatch,
    ) -> ObservationManifestReference:
        if not isinstance(batch, SecurityMasterObservationBatch):
            raise TypeError("batch must be SecurityMasterObservationBatch")
        content = _canonical_json_bytes(batch.to_manifest_dict())
        rebuilt = _rebuild_observation_manifest(
            content,
            policy=self.policy,
            require_current=False,
        )
        if rebuilt.to_manifest_dict() != batch.to_manifest_dict():
            raise SecurityMasterObservationBlockedError(
                "observation batch does not reproduce from its underlying CAS evidence"
            )
        digest = _sha256(content)
        path = self.root / "manifests" / f"{digest}.json"
        _atomic_write_exact(self.root, path, content)
        persisted = _stable_read(self.root, path, maximum=MAX_MANIFEST_BYTES)
        if persisted != content:
            raise SecurityMasterObservationBlockedError(
                "observation manifest changed during CAS read-back"
            )
        return ObservationManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def replay(
        self,
        manifest_sha256: str,
    ) -> SecurityMasterObservationBatch:
        """Replay immutable evidence without claiming that it is still current."""

        return self._replay(manifest_sha256, require_current=False)

    def replay_current(
        self,
        manifest_sha256: str,
    ) -> SecurityMasterObservationBatch:
        """Replay immutable evidence and require the live observation window."""

        return self._replay(manifest_sha256, require_current=True)

    def _replay(
        self,
        manifest_sha256: str,
        *,
        require_current: bool,
    ) -> SecurityMasterObservationBatch:
        digest = _valid_digest(manifest_sha256, "observation manifest")
        path = self.root / "manifests" / f"{digest}.json"
        content = _stable_read(self.root, path, maximum=MAX_MANIFEST_BYTES)
        if _sha256(content) != digest:
            raise SecurityMasterObservationBlockedError(
                "observation manifest hash mismatch"
            )
        return _rebuild_observation_manifest(
            content,
            policy=self.policy,
            require_current=require_current,
        )


def _rebuild_observation_manifest(
    content: bytes,
    *,
    policy: SecurityMasterObservationPolicy,
    require_current: bool,
) -> SecurityMasterObservationBatch:
    payload = _strict_canonical_json_object(content, "observation manifest")
    expected_fields = {
        "protocol_version",
        "status",
        "as_of",
        "validated_at",
        "pending_listing",
        "bse_current_delisting",
        "tdx_a_share",
        "observation_earliest_at",
        "observation_latest_at",
        "observation_span_seconds",
        "logical_content_sha256",
        "source_contract",
    }
    if set(payload) != expected_fields:
        raise SecurityMasterObservationBlockedError(
            "observation manifest schema drift"
        )
    if (
        payload["protocol_version"] != PROTOCOL_VERSION
        or payload["status"] != OBSERVATION_READY
        or payload["source_contract"] != _expected_source_contract()
    ):
        raise SecurityMasterObservationBlockedError(
            "observation manifest protocol or source contract changed"
        )
    pending_value = _strict_source_payload(payload["pending_listing"], "pending")
    bse_value = _strict_source_payload(
        payload["bse_current_delisting"], "BSE current-delisting"
    )
    tdx_value = _strict_tdx_payload(payload["tdx_a_share"])
    recorded_validation = _normalize_timestamp(
        payload["validated_at"], "recorded validation wall clock"
    )
    rebuilt = _assemble_security_master_observation(
        policy=policy,
        pending_manifest=UnderlyingManifestReference(
            cas_root=Path(pending_value["cas_root"]),
            manifest_sha256=pending_value["manifest_sha256"],
            logical_content_sha256=pending_value["logical_content_sha256"],
            claimed_earliest_retrieved_at=pending_value[
                "earliest_retrieved_at"
            ],
            claimed_latest_retrieved_at=pending_value["latest_retrieved_at"],
        ),
        bse_current_delisting_manifest=UnderlyingManifestReference(
            cas_root=Path(bse_value["cas_root"]),
            manifest_sha256=bse_value["manifest_sha256"],
            logical_content_sha256=bse_value["logical_content_sha256"],
            claimed_earliest_retrieved_at=bse_value["earliest_retrieved_at"],
            claimed_latest_retrieved_at=bse_value["latest_retrieved_at"],
        ),
        tdx_observation=TDXAShareObservation(
            observed_at=tdx_value["observed_at"],
            codes=tuple(tdx_value["codes"]),
            names=MappingProxyType(dict(tdx_value["names"])),
            code_count=tdx_value["code_count"],
            code_set_sha256=tdx_value["code_set_sha256"],
            identity_sha256=tdx_value["identity_sha256"],
        ),
        as_of=payload["as_of"],
        validation_now=recorded_validation,
    )
    if rebuilt.to_manifest_dict() != payload:
        raise SecurityMasterObservationBlockedError(
            "observation manifest does not exactly replay"
        )
    if require_current:
        _validate_batch_is_current(
            rebuilt,
            _wall_clock(),
        )
    return rebuilt


def _validate_batch_is_current(
    batch: SecurityMasterObservationBatch,
    current: datetime | str,
) -> None:
    now = _parse_timestamp(current, "replay validation wall clock")
    codes, _identities, code_digest, identity_digest = (
        _validate_tdx_identity_components(
            batch.tdx_a_share.codes,
            batch.tdx_a_share.names,
        )
    )
    if (
        batch.tdx_a_share.code_count != len(codes)
        or batch.tdx_a_share.code_set_sha256 != code_digest
        or batch.tdx_a_share.identity_sha256 != identity_digest
    ):
        raise SecurityMasterObservationBlockedError(
            "sealed TDX identity summaries changed before current replay"
        )
    as_of = _parse_timestamp(batch.as_of, "batch as_of")
    validated_at = _parse_timestamp(
        batch.validated_at, "batch validation wall clock"
    )
    tdx = _parse_timestamp(batch.tdx_a_share.observed_at, "batch TDX observed_at")
    if as_of > now + MAX_LIVE_CLOCK_SKEW or validated_at > now + MAX_LIVE_CLOCK_SKEW:
        raise SecurityMasterObservationBlockedError(
            "sealed observation is future-dated relative to the replay clock"
        )
    if now - tdx > MAX_TDX_OBSERVATION_AGE or tdx > now + MAX_LIVE_CLOCK_SKEW:
        raise SecurityMasterObservationBlockedError(
            "sealed TDX observation is stale or future-dated"
        )
    for label, evidence, maximum_age in (
        (
            "pending-listing",
            batch.pending_listing,
            pending_listing.MAX_CURRENT_EVIDENCE_AGE,
        ),
        (
            "BSE current-delisting",
            batch.bse_current_delisting,
            bse_current.MAX_CURRENT_EVIDENCE_AGE,
        ),
    ):
        earliest = _parse_timestamp(
            evidence.earliest_retrieved_at,
            f"sealed {label} earliest source time",
        )
        latest = _parse_timestamp(
            evidence.latest_retrieved_at,
            f"sealed {label} latest source time",
        )
        if latest > now + MAX_LIVE_CLOCK_SKEW:
            raise SecurityMasterObservationBlockedError(
                f"sealed {label} evidence is future-dated"
            )
        if now - earliest > maximum_age:
            raise SecurityMasterObservationBlockedError(
                f"sealed {label} evidence is stale"
            )


def _source_evidence(
    *,
    source_name: str,
    root: Path,
    protocol_version: str,
    manifest_sha256: str,
    logical_content_sha256: str,
    times: Sequence[datetime],
    target_codes: Sequence[str],
    reference: UnderlyingManifestReference,
) -> ObservationSourceEvidence:
    if not times:
        raise SecurityMasterObservationBlockedError(
            f"{source_name} contains no raw evidence timestamps"
        )
    earliest = min(times).isoformat()
    latest = max(times).isoformat()
    if reference.claimed_earliest_retrieved_at is not None and (
        _normalize_timestamp(
            reference.claimed_earliest_retrieved_at,
            f"{source_name} claimed earliest source time",
        )
        != earliest
    ):
        raise SecurityMasterObservationBlockedError(
            f"{source_name} caller earliest-time summary is forged"
        )
    if reference.claimed_latest_retrieved_at is not None and (
        _normalize_timestamp(
            reference.claimed_latest_retrieved_at,
            f"{source_name} claimed latest source time",
        )
        != latest
    ):
        raise SecurityMasterObservationBlockedError(
            f"{source_name} caller latest-time summary is forged"
        )
    return ObservationSourceEvidence(
        source_name=source_name,
        cas_root=str(root),
        protocol_version=protocol_version,
        manifest_sha256=manifest_sha256,
        logical_content_sha256=logical_content_sha256,
        earliest_retrieved_at=earliest,
        latest_retrieved_at=latest,
        raw_capture_count=len(times),
        target_codes=tuple(sorted(target_codes)),
    )


def _bse_capture_times(
    artifact: bse_current.BSECurrentDelistingArtifact,
) -> tuple[datetime, ...]:
    values: list[datetime] = []
    for notice in artifact.notices:
        values.extend(
            _parse_timestamp(item.retrieved_at, "BSE notice source time")
            for item in notice.transport_attempts
        )
    values.extend(
        _parse_timestamp(item.retrieved_at, "BSE catalogue source time")
        for item in artifact.catalogue_pages
    )
    values.append(
        _parse_timestamp(
            artifact.catalogue_closure_probe.retrieved_at,
            "BSE catalogue closure source time",
        )
    )
    return tuple(values)


def _validate_tdx_observation(
    observation: TDXAShareObservation,
    *,
    current: datetime,
    minimum_count: int,
) -> TDXAShareObservation:
    if not isinstance(observation, TDXAShareObservation):
        raise TypeError("tdx_observation must be TDXAShareObservation")
    codes, identities, code_digest, identity_digest = (
        _validate_tdx_identity_components(observation.codes, observation.names)
    )
    if (
        observation.code_count != len(codes)
        or observation.code_set_sha256 != code_digest
        or observation.identity_sha256 != identity_digest
    ):
        raise SecurityMasterObservationBlockedError(
            "TDX caller count/hash summaries do not match the exact identity map"
        )
    if len(codes) < minimum_count:
        raise SecurityMasterObservationBlockedError(
            "TDX A-share snapshot is below the configured completeness floor"
        )
    observed_at = _parse_timestamp(observation.observed_at, "TDX observed_at")
    if (
        current - observed_at > MAX_TDX_OBSERVATION_AGE
        or observed_at > current + MAX_LIVE_CLOCK_SKEW
    ):
        raise SecurityMasterObservationBlockedError(
            "TDX observed_at is stale, re-dated, or future-dated"
        )
    return TDXAShareObservation(
        observed_at=observed_at.isoformat(),
        codes=codes,
        names=_freeze_tdx_names(identities),
        code_count=len(codes),
        code_set_sha256=code_digest,
        identity_sha256=identity_digest,
    )


def _validate_tdx_codes(codes: Sequence[str]) -> tuple[str, ...]:
    if isinstance(codes, (str, bytes)) or not isinstance(codes, Sequence):
        raise SecurityMasterObservationBlockedError(
            "TDX A-share codes must be a sequence"
        )
    canonical = tuple(codes)
    if not all(
        isinstance(item, str)
        and re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", item) is not None
        for item in canonical
    ):
        raise SecurityMasterObservationBlockedError(
            "TDX A-share code format is not canonical"
        )
    if canonical != tuple(sorted(canonical)) or len(set(canonical)) != len(canonical):
        raise SecurityMasterObservationBlockedError(
            "TDX A-share codes must be uniquely sorted"
        )
    return canonical


def _validate_tdx_code(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(
        r"\d{6}\.(?:SH|SZ|BJ)", value
    ) is None:
        raise SecurityMasterObservationBlockedError(
            "TDX A-share code format is not canonical"
        )
    return value


def _validate_tdx_name(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SecurityMasterObservationBlockedError(
            f"TDX stock-list Name is missing for {code}"
        )
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise SecurityMasterObservationBlockedError(
            f"TDX stock-list Name is not canonical for {code}"
        )
    return value


def _validate_tdx_names(
    identities: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    if isinstance(identities, (str, bytes)) or not isinstance(identities, Sequence):
        raise SecurityMasterObservationBlockedError(
            "TDX identity map must be a canonical sequence"
        )
    canonical = tuple(identities)
    validated: list[tuple[str, str]] = []
    for item in canonical:
        if not isinstance(item, tuple) or len(item) != 2:
            raise SecurityMasterObservationBlockedError(
                "TDX identity map contains an invalid entry"
            )
        code = _validate_tdx_code(item[0])
        validated.append((code, _validate_tdx_name(item[1], code=code)))
    result = tuple(validated)
    codes = tuple(code for code, _name in result)
    if result != tuple(sorted(result)) or len(set(codes)) != len(codes):
        raise SecurityMasterObservationBlockedError(
            "TDX identity map must be uniquely sorted by code"
        )
    return result


def _validate_tdx_identity_components(
    codes: Sequence[str],
    names: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], str, str]:
    canonical_codes = _validate_tdx_codes(codes)
    if not isinstance(names, Mapping):
        raise SecurityMasterObservationBlockedError(
            "TDX names must be a code-to-name mapping"
        )
    identities = _validate_tdx_names(tuple(names.items()))
    name_codes = tuple(code for code, _name in identities)
    if name_codes != canonical_codes:
        raise SecurityMasterObservationBlockedError(
            "TDX code set and Name keys do not match exactly"
        )
    return (
        canonical_codes,
        identities,
        _tdx_code_set_sha256(canonical_codes),
        _tdx_identity_sha256(identities),
    )


def _freeze_tdx_names(
    identities: Sequence[tuple[str, str]],
) -> Mapping[str, str]:
    canonical = _validate_tdx_names(identities)
    return MappingProxyType({code: name for code, name in canonical})


def _tdx_code_set_sha256(codes: Sequence[str]) -> str:
    return _sha256(_canonical_json_bytes(list(codes)))


def _tdx_identity_sha256(identities: Sequence[tuple[str, str]]) -> str:
    canonical = _validate_tdx_names(identities)
    return _sha256(
        _canonical_json_bytes({code: name for code, name in canonical})
    )


def _require_fixed_root(value: Path, expected: Path, label: str) -> Path:
    actual = _lexical_absolute_path(value)
    fixed = _lexical_absolute_path(expected)
    if os.path.normcase(str(actual)) != os.path.normcase(str(fixed)):
        raise SecurityMasterObservationBlockedError(
            f"{label} CAS root does not match the fixed policy root"
        )
    _assert_no_reparse_existing_ancestors(fixed)
    if not fixed.is_dir():
        raise SecurityMasterObservationBlockedError(
            f"{label} fixed CAS root is missing"
        )
    return fixed


def _strict_source_payload(value: Any, label: str) -> Mapping[str, Any]:
    fields = {
        "source_name",
        "cas_root",
        "protocol_version",
        "manifest_sha256",
        "logical_content_sha256",
        "earliest_retrieved_at",
        "latest_retrieved_at",
        "raw_capture_count",
        "target_codes",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SecurityMasterObservationBlockedError(
            f"{label} observation source schema drift"
        )
    return value


def _strict_tdx_payload(value: Any) -> Mapping[str, Any]:
    fields = {
        "observed_at",
        "codes",
        "names",
        "code_count",
        "code_set_sha256",
        "identity_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SecurityMasterObservationBlockedError("TDX observation schema drift")
    if not isinstance(value["codes"], list):
        raise SecurityMasterObservationBlockedError("TDX code set is not a list")
    if not isinstance(value["names"], dict):
        raise SecurityMasterObservationBlockedError(
            "TDX names are not a code-to-name object"
        )
    return value


def _expected_source_contract() -> dict[str, Any]:
    return _source_contract()


def _source_contract() -> dict[str, Any]:
    return {
        "immutable": True,
        "audit_only": True,
        "collector_embedded": False,
        "publishes_security_master": False,
        "maximum_observation_window_seconds": int(
            MAX_OBSERVATION_WINDOW.total_seconds()
        ),
        "maximum_live_clock_skew_seconds": int(
            MAX_LIVE_CLOCK_SKEW.total_seconds()
        ),
        "maximum_tdx_observation_age_seconds": int(
            MAX_TDX_OBSERVATION_AGE.total_seconds()
        ),
        "pending_codes_must_be_present_in_tdx": True,
        "terminated_bse_codes_must_be_absent_from_tdx": True,
        "underlying_manifests_cold_replayed": True,
        "immutable_replay_preserves_expired_evidence": True,
        "current_gate_requires_replay_current": True,
        "tdx_identity_from_one_stock_list_response": True,
        "tdx_secondary_name_lookup_allowed": False,
        "tdx_names_required": True,
        "tdx_identity_hash_required": True,
        "tdx_identity_hash_encoding": (
            "canonical-json-sorted-code-to-name-object-utf8"
        ),
    }


def _strict_canonical_json_object(content: bytes, label: str) -> dict[str, Any]:
    if not content or len(content) > MAX_MANIFEST_BYTES:
        raise SecurityMasterObservationBlockedError(f"{label} is empty or oversized")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SecurityMasterObservationBlockedError(
                    f"{label} contains a duplicate JSON key"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=reject_duplicate,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"invalid constant {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SecurityMasterObservationBlockedError(
            f"{label} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict) or _canonical_json_bytes(value) != content:
        raise SecurityMasterObservationBlockedError(
            f"{label} is not a canonical JSON object"
        )
    return value


def _normalize_timestamp(value: datetime | str, label: str) -> str:
    return _parse_timestamp(value, label).replace(microsecond=0).isoformat()


def _parse_timestamp(value: datetime | str, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise SecurityMasterObservationBlockedError(
                f"invalid {label}: {value!r}"
            ) from exc
    else:
        raise SecurityMasterObservationBlockedError(f"invalid {label}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SecurityMasterObservationBlockedError(f"{label} needs a timezone")
    return parsed.replace(microsecond=0)


def _wall_clock() -> datetime:
    return datetime.now().astimezone()


def _valid_digest(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise SecurityMasterObservationBlockedError(f"invalid {label} SHA-256")
    return digest


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _lexical_absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path))))


def _path_is_link_or_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400))
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & reparse_flag
    )


def _assert_no_reparse_existing_ancestors(path: Path) -> None:
    absolute = _lexical_absolute_path(path)
    anchor = Path(absolute.anchor)
    current = anchor
    components = absolute.parts[1:] if absolute.anchor else absolute.parts
    if anchor.exists() and _path_is_link_or_reparse(anchor):
        raise SecurityMasterObservationBlockedError(
            "CAS path anchor is a link, junction, or reparse point"
        )
    for part in components:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if _path_is_link_or_reparse(current):
            raise SecurityMasterObservationBlockedError(
                "CAS path contains a link, junction, or reparse point"
            )


def _assert_safe_target(root: Path, target: Path, *, leaf_is_file: bool) -> None:
    root = _lexical_absolute_path(root)
    target = _lexical_absolute_path(target)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise SecurityMasterObservationBlockedError(
            "observation CAS path escapes its configured root"
        ) from exc
    _assert_no_reparse_existing_ancestors(root)
    current = root
    if not root.exists() or not root.is_dir() or _path_is_link_or_reparse(root):
        raise SecurityMasterObservationBlockedError(
            "observation CAS root is missing or unsafe"
        )
    for index, part in enumerate(relative.parts):
        if part in {"", ".", ".."}:
            raise SecurityMasterObservationBlockedError("invalid observation CAS path")
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise SecurityMasterObservationBlockedError(
                "observation CAS path is missing or unsafe"
            ) from exc
        if _path_is_link_or_reparse(current):
            raise SecurityMasterObservationBlockedError(
                "observation CAS path contains a link, junction, or reparse point"
            )
        is_leaf = index == len(relative.parts) - 1
        if leaf_is_file and is_leaf:
            if not stat.S_ISREG(metadata.st_mode):
                raise SecurityMasterObservationBlockedError(
                    "observation CAS leaf is not a regular file"
                )
        elif not stat.S_ISDIR(metadata.st_mode):
            raise SecurityMasterObservationBlockedError(
                "observation CAS parent is not a directory"
            )


def _stable_read(root: Path, path: Path, *, maximum: int) -> bytes:
    _assert_safe_target(root, path, leaf_is_file=True)
    before = os.lstat(path)
    if before.st_size < 0 or before.st_size > maximum:
        raise SecurityMasterObservationBlockedError(
            "observation CAS object is oversized"
        )
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(path, flags)
    try:
        handle_before = os.fstat(descriptor)
        if not stat.S_ISREG(handle_before.st_mode):
            raise SecurityMasterObservationBlockedError(
                "observation CAS handle is not a regular file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        handle_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.lstat(path)
    fingerprints = [
        (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            int(getattr(item, "st_file_attributes", 0)),
        )
        for item in (before, handle_before, handle_after, after)
    ]
    if len(set(fingerprints)) != 1 or _path_is_link_or_reparse(path):
        raise SecurityMasterObservationBlockedError(
            "observation CAS object changed while being read"
        )
    content = b"".join(chunks)
    if len(content) > maximum:
        raise SecurityMasterObservationBlockedError(
            "observation CAS object is oversized"
        )
    return content


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    if not content or len(content) > MAX_MANIFEST_BYTES:
        raise SecurityMasterObservationBlockedError(
            "observation manifest is empty or oversized"
        )
    root = _lexical_absolute_path(root)
    _assert_no_reparse_existing_ancestors(root)
    root.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_existing_ancestors(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_target(root, path.parent, leaf_is_file=False)
    if path.exists() or path.is_symlink():
        if _stable_read(root, path, maximum=MAX_MANIFEST_BYTES) != content:
            raise SecurityMasterObservationBlockedError(
                "immutable observation CAS collision"
            )
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0)),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_safe_target(root, temporary, leaf_is_file=True)
        os.replace(temporary, path)
        if _stable_read(root, path, maximum=MAX_MANIFEST_BYTES) != content:
            raise SecurityMasterObservationBlockedError(
                "observation CAS write verification failed"
            )
    finally:
        if temporary.exists() and not _path_is_link_or_reparse(temporary):
            temporary.unlink()


__all__ = [
    "BSE_TERMINATED_CODES",
    "DEFAULT_BSE_CURRENT_DELISTING_CAS_ROOT",
    "DEFAULT_PENDING_CAS_ROOT",
    "MAX_LIVE_CLOCK_SKEW",
    "MAX_OBSERVATION_WINDOW",
    "MAX_TDX_OBSERVATION_AGE",
    "OBSERVATION_READY",
    "OBSERVATION_REJECTED",
    "ObservationManifestReference",
    "ObservationSourceEvidence",
    "PENDING_REQUIRED_CODES",
    "PROTOCOL_VERSION",
    "SecurityMasterObservationBatch",
    "SecurityMasterObservationBlockedError",
    "SecurityMasterObservationPolicy",
    "SecurityMasterObservationStore",
    "TDX_STOCK_LIST_METHOD",
    "TDX_STOCK_LIST_PARAMS",
    "TDXAShareObservation",
    "UnderlyingManifestReference",
    "assemble_security_master_observation",
    "capture_current_security_master_observation",
]
