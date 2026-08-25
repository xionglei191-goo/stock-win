from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


PROTOCOL_VERSION = "gp29-official-status-source-capability-assessment-v1"
SOURCE_STATUS = "OFFICIAL_EVENTS_PARTIAL_GP29_UNADMITTED"
SOURCE_SCOPE = "FROZEN_THREE_DELISTED_SECURITY_SAMPLE"
AUDIT_START = "2018-01-01"
AUDIT_END = "2023-12-31"
EXPECTED_FULL_TARGET_COUNT = 239

BLOCK_AUDIT_START_ANCHOR = "AUDIT_START_STATUS_ANCHOR_MISSING"
BLOCK_ENUM_MAPPING = "GP29_STATUS_DOMAIN_MAPPING_UNVERIFIED"
BLOCK_INTERVAL_GAP = "POST_DELISTING_PERIOD_INTERVAL_SEMANTICS_UNRESOLVED"
BLOCK_STAR_SEMANTICS = "STAR_DELISTING_NOT_RISK_WARNING_BOARD_SEMANTIC_CONFLICT"
BLOCK_SOURCE_REPLAY = "SSE_SAMPLE_SOURCE_NOT_YET_COLD_REPLAYED_FROM_LOCAL_CAS"
BLOCK_FULL_SCOPE = "FULL_239_SECURITY_SCOPE_NOT_ASSESSED"

MINIMUM_EXTERNAL_EVIDENCE = (
    "OFFICIAL_EFFECTIVE_DATED_STATUS_AT_AUDIT_START_OR_LISTING_DATE_PER_SECURITY",
    "OFFICIAL_GP29_NUMERIC_DOMAIN_TO_NORMAL_ST_STAR_ST_DELISTING_MAPPING",
    "OFFICIAL_STATUS_FOR_DAYS_AFTER_DELISTING_PERIOD_END_BEFORE_DELIST_DATE",
    "OFFICIAL_SSE_STAR_DELISTING_TO_GP29_SEMANTIC_RULING",
    "COLD_REPLAYED_SOURCE_DOCUMENTS_AND_COMPLETE_INDEXES_FOR_ALL_239_SECURITIES",
)

_ASSESSMENT_SEAL = object()
_SHA256 = re.compile(r"[0-9a-f]{64}")


class GP29StatusSourceAssessmentBlockedError(RuntimeError):
    """The frozen source-capability assessment was changed or overclaimed."""


@dataclass(frozen=True)
class OfficialStatusEventEvidence:
    event_id: str
    event_type: str
    published_at: str
    effective_date: str
    through_date_inclusive: str | None
    delisted_date: str | None
    title: str
    source_url: str
    source_document_sha256: str
    raw_document_cold_replayed: bool
    statement: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SampleSourceObservation:
    exchange: str
    code: str
    board: str
    listing_valid_from: str
    listing_valid_to: str
    index_authority: str
    index_endpoint: str
    index_query_start: str
    index_query_end: str
    index_page_count: int
    index_row_count: int
    pagination_total_closed: bool
    index_cold_replayed: bool
    index_manifest_sha256: str | None
    audit_start_anchor_sha256: str | None
    events: tuple[OfficialStatusEventEvidence, ...]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["events"] = [item.to_dict() for item in self.events]
        value["blockers"] = list(self.blockers)
        return value


@dataclass(frozen=True)
class GP29StatusSourceCapabilityAssessment:
    observations: tuple[SampleSourceObservation, ...]
    logical_content_sha256: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ASSESSMENT_SEAL:
            raise TypeError("GP29 source assessments must be built by the frozen builder")

    @property
    def ready(self) -> bool:
        return False

    @property
    def quality_rows_emitted(self) -> int:
        return 0

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": False,
            "status": SOURCE_STATUS,
            "scope": SOURCE_SCOPE,
            "audit_start": AUDIT_START,
            "audit_end": AUDIT_END,
            "expected_full_target_count": EXPECTED_FULL_TARGET_COUNT,
            "sample_only": True,
            "official_event_references_only": True,
            "caller_ready_attestation_allowed": False,
            "quality_dataset_eligibility": False,
            "gp29_rows_may_be_emitted": False,
            "training_allowed": False,
            "label_generation_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
        }

    @property
    def coverage(self) -> dict[str, Any]:
        blockers = sorted(
            {blocker for item in self.observations for blocker in item.blockers}
        )
        return {
            "sample_target_count": len(self.observations),
            "expected_full_target_count": EXPECTED_FULL_TARGET_COUNT,
            "sample_index_pagination_closed_count": sum(
                item.pagination_total_closed for item in self.observations
            ),
            "sample_index_cold_replayed_count": sum(
                item.index_cold_replayed for item in self.observations
            ),
            "sample_delisting_window_event_count": sum(
                bool(item.events) for item in self.observations
            ),
            "sample_audit_start_anchor_count": sum(
                item.audit_start_anchor_sha256 is not None
                for item in self.observations
            ),
            "sample_full_interval_semantics_resolved_count": sum(
                not any(
                    blocker
                    in {BLOCK_AUDIT_START_ANCHOR, BLOCK_ENUM_MAPPING, BLOCK_INTERVAL_GAP,
                        BLOCK_STAR_SEMANTICS}
                    for blocker in item.blockers
                )
                for item in self.observations
            ),
            "quality_rows_emitted": 0,
            "full_scope_security_count_admitted": 0,
            "blockers": blockers,
        }

    @property
    def minimum_external_evidence(self) -> tuple[str, ...]:
        return MINIMUM_EXTERNAL_EVIDENCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "ready": False,
            "quality_rows_emitted": 0,
            "observations": [item.to_dict() for item in self.observations],
            "coverage": self.coverage,
            "minimum_external_evidence": list(self.minimum_external_evidence),
            "source_contract": self.source_contract,
            "logical_content_sha256": self.logical_content_sha256,
        }


def _event(
    event_id: str,
    event_type: str,
    published_at: str,
    effective_date: str,
    through_date_inclusive: str | None,
    delisted_date: str | None,
    title: str,
    source_url: str,
    source_document_sha256: str,
    raw_document_cold_replayed: bool,
    statement: str,
) -> OfficialStatusEventEvidence:
    return OfficialStatusEventEvidence(
        event_id=event_id,
        event_type=event_type,
        published_at=published_at,
        effective_date=effective_date,
        through_date_inclusive=through_date_inclusive,
        delisted_date=delisted_date,
        title=title,
        source_url=source_url,
        source_document_sha256=source_document_sha256,
        raw_document_cold_replayed=raw_document_cold_replayed,
        statement=statement,
    )


def frozen_sample_observations() -> tuple[SampleSourceObservation, ...]:
    """Return observed official events without converting them to GP29 rows.

    Complete announcement pagination proves that the referenced event documents
    are not isolated search hits.  It does not prove the status at the audit
    boundary, nor does a company name in an announcement license backward-fill.
    """

    return (
        SampleSourceObservation(
            exchange="SSE",
            code="600432.SH",
            board="SSE_MAIN",
            listing_valid_from="2003-09-05",
            listing_valid_to="2018-07-13",
            index_authority="SSE_OFFICIAL_DISCLOSURE",
            index_endpoint=(
                "https://query.sse.com.cn/security/stock/"
                "queryCompanyBulletin.do"
            ),
            index_query_start="2003-09-05",
            index_query_end="2018-07-13",
            index_page_count=11,
            index_row_count=1092,
            pagination_total_closed=True,
            index_cold_replayed=False,
            index_manifest_sha256=None,
            audit_start_anchor_sha256=None,
            events=(
                _event(
                    "SSE:600432:2018-05-23:DELISTING_PERIOD",
                    "DELISTING_PERIOD_STARTED",
                    "2018-05-23T00:00:00+08:00",
                    "2018-05-30",
                    "2018-07-11",
                    "2018-07-13",
                    "*ST\u5409\u6069\u5173\u4e8e\u516c\u53f8\u80a1\u7968\u8fdb\u5165\u9000\u5e02\u6574\u7406\u671f\u4ea4\u6613\u7684\u516c\u544a",
                    "https://static.sse.com.cn/disclosure/listedinfo/announcement/"
                    "c/2018-05-23/600432_20180523_2.pdf",
                    "cd47343136aba4db48e4ca2c1e23931aff7f4b314746ca49334d413a208b2604",
                    False,
                    "Official notice states 2018-05-30 start and expected 2018-07-11 last session.",
                ),
                _event(
                    "SSE:600432:2018-07-12:DELISTED",
                    "DELISTING_PERIOD_ENDED_AND_DELIST_DATE_CONFIRMED",
                    "2018-07-12T00:00:00+08:00",
                    "2018-07-12",
                    "2018-07-11",
                    "2018-07-13",
                    "\u9000\u5e02\u5409\u6069\u5173\u4e8e\u6574\u7406\u671f\u7ed3\u675f\u53ca\u6458\u724c\u66a8\u540e\u7eed\u6709\u5173\u4e8b\u9879\u5b89\u6392\u7684\u516c\u544a",
                    "https://static.sse.com.cn/disclosure/listedinfo/announcement/"
                    "c/2018-07-12/600432_20180712_1.pdf",
                    "6765ec0af0e0eac370337eb22a0925cf51efa3ee4b6c52ef565913b439663202",
                    False,
                    "Official notice confirms 2018-07-11 period end and 2018-07-13 delisting.",
                ),
            ),
            blockers=(
                BLOCK_AUDIT_START_ANCHOR,
                BLOCK_ENUM_MAPPING,
                BLOCK_INTERVAL_GAP,
                BLOCK_SOURCE_REPLAY,
                BLOCK_FULL_SCOPE,
            ),
        ),
        SampleSourceObservation(
            exchange="SZSE",
            code="000511.SZ",
            board="SZSE_MAIN",
            listing_valid_from="1993-05-18",
            listing_valid_to="2018-07-18",
            index_authority="CNINFO_SZSE_OFFICIAL_DISCLOSURE",
            index_endpoint="https://www.cninfo.com.cn/new/hisAnnouncement/query",
            index_query_start="2018-01-01",
            index_query_end="2018-07-17",
            index_page_count=3,
            index_row_count=79,
            pagination_total_closed=True,
            index_cold_replayed=True,
            index_manifest_sha256=(
                "fb645ba1c60560ba31897f8c05f991e96c97656946b7c928bdf0d4152868d979"
            ),
            audit_start_anchor_sha256=None,
            events=(
                _event(
                    "CNINFO:1205010494",
                    "DELISTING_PERIOD_STARTED",
                    "2018-05-29T00:00:00+08:00",
                    "2018-06-05",
                    "2018-07-17",
                    "2018-07-18",
                    "\u5173\u4e8e\u516c\u53f8\u80a1\u7968\u8fdb\u5165\u9000\u5e02\u6574\u7406\u671f\u4ea4\u6613\u7684\u516c\u544a",
                    "https://static.cninfo.com.cn/finalpage/2018-05-29/1205010494.PDF",
                    "41241acdcad3417ab13022c6aad54757b0cc74f0254bc7a3c1b154a7d42fe0a1",
                    True,
                    "Official notice states 2018-06-05 start and expected 2018-07-17 last session.",
                ),
                _event(
                    "CNINFO:1205169251",
                    "DELISTING_PERIOD_LAST_SESSION_CONFIRMED",
                    "2018-07-17T00:00:00+08:00",
                    "2018-07-17",
                    "2018-07-17",
                    "2018-07-18",
                    "\u5173\u4e8e\u516c\u53f8\u80a1\u7968\u8fdb\u5165\u9000\u5e02\u6574\u7406\u671f\u4ea4\u6613\u7684\u6700\u540e\u4e00\u4e2a\u4ea4\u6613\u65e5\u7684\u98ce\u9669\u63d0\u793a\u516c\u544a",
                    "https://static.cninfo.com.cn/finalpage/2018-07-17/1205169251.PDF",
                    "469e86be467d352de1d3d332c29aef4c44078e571e633126e349917515516ac0",
                    True,
                    "Official notice confirms 2018-07-17 as the last delisting-period session.",
                ),
            ),
            blockers=(
                BLOCK_AUDIT_START_ANCHOR,
                BLOCK_ENUM_MAPPING,
                BLOCK_FULL_SCOPE,
            ),
        ),
        SampleSourceObservation(
            exchange="SSE",
            code="688086.SH",
            board="STAR",
            listing_valid_from="2020-02-26",
            listing_valid_to="2023-07-07",
            index_authority="SSE_OFFICIAL_DISCLOSURE",
            index_endpoint=(
                "https://query.sse.com.cn/security/stock/"
                "queryCompanyBulletin.do"
            ),
            index_query_start="2020-02-26",
            index_query_end="2023-07-07",
            index_page_count=5,
            index_row_count=492,
            pagination_total_closed=True,
            index_cold_replayed=False,
            index_manifest_sha256=None,
            audit_start_anchor_sha256=None,
            events=(
                _event(
                    "SSE:688086:2023-06-01:DELISTING_PERIOD",
                    "DELISTING_PERIOD_STARTED",
                    "2023-06-01T00:00:00+08:00",
                    "2023-06-08",
                    "2023-06-30",
                    "2023-07-07",
                    "\u5173\u4e8e\u516c\u53f8\u80a1\u7968\u8fdb\u5165\u9000\u5e02\u6574\u7406\u671f\u4ea4\u6613\u7684\u516c\u544a",
                    "https://static.sse.com.cn/disclosure/listedinfo/announcement/"
                    "c/new/2023-06-01/688086_20230601_8EU4.pdf",
                    "1464037f6944cec2151f22e08f42a26adaa6dd2dc178d86806b014c80949b8df",
                    False,
                    "Official notice states 2023-06-08 start and expected 2023-06-30 last session.",
                ),
                _event(
                    "SSE:688086:2023-06-14:NOT_RISK_WARNING_BOARD",
                    "STAR_DELISTING_BOARD_SEMANTIC",
                    "2023-06-14T00:00:00+08:00",
                    "2023-06-14",
                    "2023-06-30",
                    "2023-07-07",
                    "\u5173\u4e8e\u516c\u53f8\u80a1\u7968\u8fdb\u5165\u9000\u5e02\u6574\u7406\u671f\u4ea4\u6613\u7684\u7b2c\u4e8c\u6b21\u98ce\u9669\u63d0\u793a\u516c\u544a",
                    "https://static.sse.com.cn/disclosure/listedinfo/announcement/"
                    "c/new/2023-06-14/688086_20230614_746M.pdf",
                    "72a92f16e26d6afd398db624016d9b046571090ad1a43755c2cf3db379845eea",
                    False,
                    "Official notice states STAR delisting-period shares do not enter the risk-warning board.",
                ),
                _event(
                    "SSE:688086:2023-07-01:DELISTED",
                    "DELISTING_PERIOD_ENDED_AND_DELIST_DATE_CONFIRMED",
                    "2023-07-01T00:00:00+08:00",
                    "2023-07-01",
                    "2023-06-30",
                    "2023-07-07",
                    "\u5173\u4e8e\u516c\u53f8\u80a1\u7968\u7ec8\u6b62\u4e0a\u5e02\u66a8\u6458\u724c\u7684\u516c\u544a",
                    "https://static.sse.com.cn/disclosure/listedinfo/announcement/"
                    "c/new/2023-07-01/688086_20230701_TM0J.pdf",
                    "5c5c9b50420722a0db3202b01de0e95a3ad05a74f1845899913f5111fc30bac8",
                    False,
                    "Official notice confirms 2023-06-30 period end and 2023-07-07 delisting.",
                ),
            ),
            blockers=(
                BLOCK_AUDIT_START_ANCHOR,
                BLOCK_ENUM_MAPPING,
                BLOCK_INTERVAL_GAP,
                BLOCK_STAR_SEMANTICS,
                BLOCK_SOURCE_REPLAY,
                BLOCK_FULL_SCOPE,
            ),
        ),
    )


def build_frozen_gp29_source_capability_assessment(
) -> GP29StatusSourceCapabilityAssessment:
    observations = frozen_sample_observations()
    _validate_observations(observations)
    logical_payload = {
        "protocol_version": PROTOCOL_VERSION,
        "observations": [item.to_dict() for item in observations],
        "minimum_external_evidence": list(MINIMUM_EXTERNAL_EVIDENCE),
    }
    logical_hash = _sha256(_canonical_json_bytes(logical_payload))
    artifact = GP29StatusSourceCapabilityAssessment(
        observations=observations,
        logical_content_sha256=logical_hash,
        _seal=_ASSESSMENT_SEAL,
    )
    # Recompute after construction so property changes cannot silently upgrade it.
    if artifact.ready or artifact.quality_rows_emitted != 0:
        raise GP29StatusSourceAssessmentBlockedError(
            "source-capability assessment attempted to emit admissible GP29 data"
        )
    return artifact


def replay_frozen_gp29_source_capability_assessment(
    value: Mapping[str, Any],
) -> GP29StatusSourceCapabilityAssessment:
    artifact = build_frozen_gp29_source_capability_assessment()
    expected = artifact.to_dict()
    if dict(value) != expected:
        raise GP29StatusSourceAssessmentBlockedError(
            "GP29 source-capability assessment did not replay exactly"
        )
    return artifact


def _validate_observations(observations: Sequence[SampleSourceObservation]) -> None:
    expected_codes = ("000511.SZ", "600432.SH", "688086.SH")
    if tuple(sorted(item.code for item in observations)) != expected_codes:
        raise GP29StatusSourceAssessmentBlockedError(
            "frozen GP29 sample scope changed"
        )
    for item in observations:
        if item.exchange not in {"SSE", "SZSE"}:
            raise GP29StatusSourceAssessmentBlockedError("unsupported exchange")
        if not item.pagination_total_closed or item.index_page_count <= 0 or item.index_row_count <= 0:
            raise GP29StatusSourceAssessmentBlockedError(
                f"announcement index is not pagination-closed for {item.code}"
            )
        _iso_date(item.listing_valid_from, "listing_valid_from")
        _iso_date(item.listing_valid_to, "listing_valid_to")
        _iso_date(item.index_query_start, "index_query_start")
        _iso_date(item.index_query_end, "index_query_end")
        if item.audit_start_anchor_sha256 is not None:
            raise GP29StatusSourceAssessmentBlockedError(
                f"frozen sample unexpectedly claims an audit-start anchor for {item.code}"
            )
        required = {BLOCK_AUDIT_START_ANCHOR, BLOCK_ENUM_MAPPING, BLOCK_FULL_SCOPE}
        if not required.issubset(item.blockers):
            raise GP29StatusSourceAssessmentBlockedError(
                f"mandatory fail-closed blocker missing for {item.code}"
            )
        if item.code == "688086.SH" and BLOCK_STAR_SEMANTICS not in item.blockers:
            raise GP29StatusSourceAssessmentBlockedError(
                "STAR delisting semantic conflict was removed"
            )
        if not item.events:
            raise GP29StatusSourceAssessmentBlockedError(
                f"sample has no official status event references: {item.code}"
            )
        for event in item.events:
            _validate_event(item, event)
        if item.index_cold_replayed:
            _sha256_text(item.index_manifest_sha256, "index manifest")
        elif item.index_manifest_sha256 is not None:
            raise GP29StatusSourceAssessmentBlockedError(
                "non-replayed index cannot claim a manifest"
            )


def _validate_event(
    observation: SampleSourceObservation, event: OfficialStatusEventEvidence
) -> None:
    if not event.event_id or not event.event_type or not event.title or not event.statement:
        raise GP29StatusSourceAssessmentBlockedError("event identity is incomplete")
    published = event.published_at
    try:
        published_date = date.fromisoformat(published[:10])
    except (TypeError, ValueError) as exc:
        raise GP29StatusSourceAssessmentBlockedError(
            f"invalid publication date for {event.event_id}"
        ) from exc
    effective = _iso_date(event.effective_date, "effective_date")
    if published_date > effective:
        raise GP29StatusSourceAssessmentBlockedError(
            f"event is published after its claimed effective date: {event.event_id}"
        )
    if event.through_date_inclusive is not None:
        through = _iso_date(event.through_date_inclusive, "through_date_inclusive")
        if through < effective and event.event_type == "DELISTING_PERIOD_STARTED":
            raise GP29StatusSourceAssessmentBlockedError(
                f"delisting event dates are reversed: {event.event_id}"
            )
    if event.delisted_date is not None:
        delisted = _iso_date(event.delisted_date, "delisted_date")
        if delisted != _iso_date(observation.listing_valid_to, "listing_valid_to"):
            raise GP29StatusSourceAssessmentBlockedError(
                f"event delist date is not bound to master interval: {event.event_id}"
            )
    parsed = urlsplit(event.source_url)
    admitted_hosts = (
        {"static.sse.com.cn"}
        if observation.exchange == "SSE"
        else {"static.cninfo.com.cn", "www.cninfo.com.cn"}
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname not in admitted_hosts
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GP29StatusSourceAssessmentBlockedError(
            f"event source escaped official HTTPS origin: {event.event_id}"
        )
    _sha256_text(event.source_document_sha256, "source document")


def _iso_date(value: str, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise GP29StatusSourceAssessmentBlockedError(
            f"{field_name} is not an ISO-8601 date"
        ) from exc
    if parsed.isoformat() != value:
        raise GP29StatusSourceAssessmentBlockedError(
            f"{field_name} is not canonical"
        )
    return parsed


def _sha256_text(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if _SHA256.fullmatch(digest) is None:
        raise GP29StatusSourceAssessmentBlockedError(f"invalid {label} SHA-256")
    return digest


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "AUDIT_END",
    "AUDIT_START",
    "BLOCK_AUDIT_START_ANCHOR",
    "BLOCK_ENUM_MAPPING",
    "BLOCK_FULL_SCOPE",
    "BLOCK_INTERVAL_GAP",
    "BLOCK_SOURCE_REPLAY",
    "BLOCK_STAR_SEMANTICS",
    "EXPECTED_FULL_TARGET_COUNT",
    "GP29StatusSourceAssessmentBlockedError",
    "GP29StatusSourceCapabilityAssessment",
    "MINIMUM_EXTERNAL_EVIDENCE",
    "OfficialStatusEventEvidence",
    "PROTOCOL_VERSION",
    "SOURCE_SCOPE",
    "SOURCE_STATUS",
    "SampleSourceObservation",
    "build_frozen_gp29_source_capability_assessment",
    "frozen_sample_observations",
    "replay_frozen_gp29_source_capability_assessment",
]
