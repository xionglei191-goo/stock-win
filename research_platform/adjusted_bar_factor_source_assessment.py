from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


PROTOCOL_VERSION = "adjusted-bar-factor-source-capability-assessment-v1"
SOURCE_STATUS = "ADJUSTED_BAR_FACTOR_SOURCE_UNAVAILABLE"
SOURCE_SCOPE = "FROZEN_239_DELISTED_SECURITY_SCOPE"
AUDIT_START = "2018-01-01"
AUDIT_END = "2023-12-31"
EXPECTED_FULL_TARGET_COUNT = 239
EXPECTED_EXCHANGE_COUNTS = {"SSE": 99, "SZSE": 140}

ADJUSTED_BAR_FACTOR_SCHEMA = (
    "exchange",
    "code",
    "trade_date",
    "front_open",
    "front_high",
    "front_low",
    "front_close",
    "adjustment_factor",
    "anchor_trade_date",
    "anchor_adjustment_factor",
)

RAW_FRONT_RELATIVE_TOLERANCE = 1e-9
RAW_FRONT_ABSOLUTE_TOLERANCE = 1e-6
FACTOR_CHANGE_RELATIVE_TOLERANCE = 1e-12
FACTOR_CHANGE_ABSOLUTE_TOLERANCE = 1e-12

SSE_RAW_SOURCE_INDEX_SHA256 = (
    "4444e219c7aa9f7db0fa238e1f1107d0c43a6b5e064430be0d55d3156d123dea"
)
CORPORATE_ACTION_FAILURE_MANIFEST_SHA256 = (
    "b7aa06cab7f5b7d8e9c608ad793b99a4b87e4ec6b2862b2edae11c6cba3e76a1"
)
SSE_DIVIDEND_CORROBORATION_MANIFEST_SHA256 = (
    "b8cd408138d5b13512185a3721fdaa889da22a3f85575cb30f60af0d46361f9e"
)

BLOCK_RAW_SCOPE = "RAW_EXECUTION_BAR_SCOPE_INCOMPLETE_56_OF_239"
BLOCK_FACTOR_SOURCE = "AUTHORIZED_DAILY_FACTOR_AND_FRONT_BAR_SOURCE_MISSING"
BLOCK_2017_ANCHOR = "PRE_AUDIT_2017_FACTOR_ANCHORS_MISSING"
BLOCK_GP30 = "GP30_CORPORATE_ACTION_QUALITY_ROWS_MISSING"
BLOCK_GP43 = "GP43_CORPORATE_ACTION_QUALITY_ROWS_MISSING"
BLOCK_DUAL_SOURCE = "INDEPENDENT_GP30_GP43_EVENT_RECONCILIATION_MISSING"
BLOCK_FACTOR_CHANGES = "FACTOR_CHANGE_TO_DUAL_SOURCE_EVENT_CLOSURE_MISSING"
BLOCK_ROW_PROVENANCE = "DAILY_FACTOR_DERIVATION_PROVENANCE_MISSING"

BLOCKERS = (
    BLOCK_RAW_SCOPE,
    BLOCK_FACTOR_SOURCE,
    BLOCK_2017_ANCHOR,
    BLOCK_GP30,
    BLOCK_GP43,
    BLOCK_DUAL_SOURCE,
    BLOCK_FACTOR_CHANGES,
    BLOCK_ROW_PROVENANCE,
)

PROHIBITED_INFERENCES = (
    "TDX_EMPTY_NULL_OR_UNAVAILABLE_FACTOR_AS_FACTOR_ONE_OR_ZERO",
    "TDX_ADJUSTED_BAR_WITHOUT_HASH_BOUND_FACTOR_LINEAGE_AS_OFFICIAL_EVIDENCE",
    "RAW_PRICE_GAP_OR_OHLC_DISCONTINUITY_AS_A_FACTOR_OR_FACTOR_CHANGE",
    "RAW_BARS_ALONE_AS_FRONT_ADJUSTED_BARS",
    "SINGLE_ANNOUNCEMENT_OR_SINGLE_CORPORATE_ACTION_SOURCE_AS_A_FACTOR",
    "COPYING_ONE_DOCUMENT_OR_UPSTREAM_RECORD_INTO_BOTH_GP30_AND_GP43",
    "SSE_CASH_DIVIDEND_CORROBORATION_AS_COMPLETE_CORPORATE_ACTION_HISTORY",
    "CURRENT_FACTOR_BACKFILL_ACROSS_2017_2023",
    "VENDOR_ADJUSTED_CLOSE_RATIO_WITHOUT_METHOD_AND_SOURCE_PROVENANCE",
)

MINIMUM_AUTHORIZED_SOURCES = (
    "SSE_AUTHORIZED_RAW_AND_FRONT_ADJUSTED_DAILY_ARCHIVE_WITH_PER_BAR_FACTORS_FOR_ALL_99_TARGETS_INCLUDING_2017_ANCHORS",
    "SZSE_AUTHORIZED_RAW_AND_FRONT_ADJUSTED_DAILY_ARCHIVE_WITH_PER_BAR_FACTORS_FOR_ALL_140_TARGETS_INCLUDING_2017_ANCHORS",
    "GP30_OFFICIAL_OR_AUTHORIZED_POINT_IN_TIME_CORPORATE_ACTION_HISTORY_FOR_ALL_239_TARGETS",
    "GP43_INDEPENDENT_OFFICIAL_OR_AUTHORIZED_POINT_IN_TIME_CORPORATE_ACTION_HISTORY_FOR_ALL_239_TARGETS",
)

MINIMUM_EXTERNAL_EVIDENCE = (
    "ONE_POSITIVE_FINITE_ADJUSTMENT_FACTOR_AND_FRONT_OHLC_ROW_FOR_EVERY_RAW_TRADABLE_BAR",
    "A_PRE_FIRST_PARTITION_FACTOR_ANCHOR_AND_A_2017_LAST_TRADING_DAY_ANCHOR_FOR_EACH_SECURITY_ACTIVE_AT_THE_2018_BOUNDARY",
    "CONTENT_ADDRESSED_RAW_AND_FRONT_BAR_INPUTS_PROVING_FRONT_OHLC_EQUALS_RAW_OHLC_TIMES_FACTOR",
    "INDEPENDENT_GP30_AND_GP43_EVENT_ROWS_WITH_PUBLISHED_AT_EFFECTIVE_AT_AND_SOURCE_DOCUMENT_HASH",
    "EXACT_GP30_GP43_AGREEMENT_ON_EXCHANGE_CODE_EVENT_TYPE_EX_DATE_RATIO_AND_CASH_AMOUNT",
    "EXACT_SET_EQUALITY_BETWEEN_FACTOR_CHANGE_DATES_AND_RECONCILED_CORPORATE_ACTION_EX_DATES",
    "CONTENT_ADDRESSED_DERIVATION_MANIFEST_BINDING_EVERY_DAILY_FACTOR_ANCHOR_RAW_BAR_FRONT_BAR_AND_EVENT_INPUT",
    "COMPLETE_2018_2023_PARTITIONS_FOR_ALL_239_SECURITIES_WITH_NO_RAW_OR_ADJUSTED_ORPHANS",
)

_ASSESSMENT_SEAL = object()
_SHA256 = re.compile(r"[0-9a-f]{64}")


class AdjustedBarFactorSourceAssessmentBlockedError(RuntimeError):
    """The frozen factor-source assessment was changed or overclaimed."""


@dataclass(frozen=True)
class ExchangeCoverageObservation:
    exchange: str
    target_security_count: int
    official_raw_security_count: int
    raw_security_missing_count: int
    raw_source_index_present: bool
    raw_source_index_sha256: str | None
    admitted_factor_security_count: int
    admitted_factor_row_count: int
    admitted_2017_anchor_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CorporateActionFailureEvidence:
    evidence_id: str
    manifest_sha256: str
    target_security_count: int
    expected_security_count: int
    candidate_or_corroboration_row_count: int
    gp30_quality_row_count: int
    gp43_quality_row_count: int
    factor_eligible_event_count: int
    ready: bool
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["limitations"] = list(self.limitations)
        return value


@dataclass(frozen=True)
class FactorAdmissionRequirement:
    requirement_id: str
    grain: str
    required_evidence: str
    hard_check: str
    current_status: str
    admitted_row_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuthorizedSourceRequirement:
    source_id: str
    exchange: str
    target_security_count: int
    minimum_capability: str
    current_authorized_security_count: int
    current_status: str
    independence_requirement: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AdjustedBarFactorSourceCapabilityAssessment:
    exchange_coverage: tuple[ExchangeCoverageObservation, ...]
    corporate_action_failure_evidence: tuple[CorporateActionFailureEvidence, ...]
    admission_requirements: tuple[FactorAdmissionRequirement, ...]
    authorized_source_requirements: tuple[AuthorizedSourceRequirement, ...]
    logical_content_sha256: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ASSESSMENT_SEAL:
            raise TypeError(
                "adjusted-bar factor assessments must be built by the frozen builder"
            )

    @property
    def ready(self) -> bool:
        return False

    @property
    def quality_rows_emitted(self) -> int:
        return 0

    @property
    def quality_row_count(self) -> int:
        return 0

    @property
    def training_allowed(self) -> bool:
        return False

    @property
    def trading_allowed(self) -> bool:
        return False

    @property
    def promotion_allowed(self) -> bool:
        return False

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": False,
            "status": SOURCE_STATUS,
            "scope": SOURCE_SCOPE,
            "audit_start": AUDIT_START,
            "audit_end": AUDIT_END,
            "expected_full_target_count": EXPECTED_FULL_TARGET_COUNT,
            "schema": list(ADJUSTED_BAR_FACTOR_SCHEMA),
            "source_authority_by_exchange": {
                "SSE": "SSE_CORPORATE_ACTION_ADJUSTMENT_AUDIT",
                "SZSE": "SZSE_CORPORATE_ACTION_ADJUSTMENT_AUDIT",
            },
            "caller_ready_attestation_allowed": False,
            "quality_dataset_eligibility": False,
            "adjusted_bar_factor_rows_may_be_emitted": False,
            "training_allowed": False,
            "label_generation_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
            "tdx_empty_value_inference_allowed": False,
            "price_discontinuity_inference_allowed": False,
            "single_corporate_action_source_inference_allowed": False,
            "same_upstream_dual_source_claim_allowed": False,
        }

    @property
    def arithmetic_contract(self) -> dict[str, Any]:
        return {
            "grain": "exchange,code,trade_date",
            "equations": [
                "front_open=raw_open*adjustment_factor",
                "front_high=raw_high*adjustment_factor",
                "front_low=raw_low*adjustment_factor",
                "front_close=raw_close*adjustment_factor",
            ],
            "factor_must_be_positive_and_finite": True,
            "front_ohlc_must_be_positive_and_finite": True,
            "relative_tolerance": RAW_FRONT_RELATIVE_TOLERANCE,
            "absolute_tolerance": RAW_FRONT_ABSOLUTE_TOLERANCE,
            "raw_bar_without_adjusted_row_allowed": False,
            "adjusted_row_without_raw_bar_allowed": False,
        }

    @property
    def factor_change_contract(self) -> dict[str, Any]:
        return {
            "first_comparison_value": "anchor_adjustment_factor",
            "anchor_trade_date_must_precede_first_partition_trade_date": True,
            "anchor_fields_only_on_first_partition_row": True,
            "2017_anchor_required_for_security_active_at_2018_boundary": True,
            "relative_tolerance": FACTOR_CHANGE_RELATIVE_TOLERANCE,
            "absolute_tolerance": FACTOR_CHANGE_ABSOLUTE_TOLERANCE,
            "gp30_gp43_reconciliation_key": [
                "exchange",
                "code",
                "event_type",
                "ex_date",
                "ratio",
                "cash_amount",
            ],
            "gp30_gp43_sources_must_be_independent": True,
            "each_event_requires_published_at_effective_at_and_source_document_hash": True,
            "factor_change_dates_must_equal_reconciled_event_ex_dates": True,
            "factor_change_without_event_allowed": False,
            "event_without_factor_change_allowed": False,
        }

    @property
    def coverage(self) -> dict[str, Any]:
        target_count = sum(item.target_security_count for item in self.exchange_coverage)
        raw_count = sum(
            item.official_raw_security_count for item in self.exchange_coverage
        )
        factor_count = sum(
            item.admitted_factor_security_count for item in self.exchange_coverage
        )
        by_exchange = {
            item.exchange: {
                "target_security_count": item.target_security_count,
                "official_raw_security_count": item.official_raw_security_count,
                "raw_security_missing_count": item.raw_security_missing_count,
                "raw_security_coverage_ratio": (
                    item.official_raw_security_count / item.target_security_count
                ),
                "raw_source_index_present": item.raw_source_index_present,
                "raw_source_index_sha256": item.raw_source_index_sha256,
                "admitted_factor_security_count": item.admitted_factor_security_count,
                "admitted_factor_row_count": item.admitted_factor_row_count,
                "admitted_2017_anchor_count": item.admitted_2017_anchor_count,
            }
            for item in self.exchange_coverage
        }
        return {
            "target_security_count": target_count,
            "target_exchange_counts": {
                item.exchange: item.target_security_count
                for item in self.exchange_coverage
            },
            "by_exchange": by_exchange,
            "official_raw_security_count": raw_count,
            "raw_security_missing_count": target_count - raw_count,
            "raw_security_coverage_ratio": raw_count / target_count,
            "admitted_factor_security_count": factor_count,
            "factor_security_coverage_ratio": factor_count / target_count,
            "admitted_factor_row_count": 0,
            "admitted_2017_anchor_count": 0,
            "gp30_quality_row_count": 0,
            "gp43_quality_row_count": 0,
            "reconciled_dual_source_event_count": 0,
            "factor_eligible_event_count": 0,
            "quality_rows_emitted": 0,
            "quality_row_count": 0,
            "full_scope_daily_coverage_closed": False,
            "full_scope_security_count_admitted": 0,
            "blockers": list(BLOCKERS),
        }

    @property
    def prohibited_inferences(self) -> tuple[str, ...]:
        return PROHIBITED_INFERENCES

    @property
    def minimum_authorized_sources(self) -> tuple[str, ...]:
        return MINIMUM_AUTHORIZED_SOURCES

    @property
    def minimum_external_evidence(self) -> tuple[str, ...]:
        return MINIMUM_EXTERNAL_EVIDENCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "ready": False,
            "quality_rows_emitted": 0,
            "quality_row_count": 0,
            "training_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
            "exchange_coverage": [item.to_dict() for item in self.exchange_coverage],
            "corporate_action_failure_evidence": [
                item.to_dict() for item in self.corporate_action_failure_evidence
            ],
            "admission_requirements": [
                item.to_dict() for item in self.admission_requirements
            ],
            "authorized_source_requirements": [
                item.to_dict() for item in self.authorized_source_requirements
            ],
            "arithmetic_contract": self.arithmetic_contract,
            "factor_change_contract": self.factor_change_contract,
            "coverage": self.coverage,
            "minimum_authorized_sources": list(self.minimum_authorized_sources),
            "minimum_external_evidence": list(self.minimum_external_evidence),
            "prohibited_inferences": list(self.prohibited_inferences),
            "source_contract": self.source_contract,
            "logical_content_sha256": self.logical_content_sha256,
        }


def frozen_exchange_coverage() -> tuple[ExchangeCoverageObservation, ...]:
    """Freeze observed raw coverage without treating it as factor evidence."""

    return (
        ExchangeCoverageObservation(
            exchange="SSE",
            target_security_count=99,
            official_raw_security_count=56,
            raw_security_missing_count=43,
            raw_source_index_present=True,
            raw_source_index_sha256=SSE_RAW_SOURCE_INDEX_SHA256,
            admitted_factor_security_count=0,
            admitted_factor_row_count=0,
            admitted_2017_anchor_count=0,
        ),
        ExchangeCoverageObservation(
            exchange="SZSE",
            target_security_count=140,
            official_raw_security_count=0,
            raw_security_missing_count=140,
            raw_source_index_present=False,
            raw_source_index_sha256=None,
            admitted_factor_security_count=0,
            admitted_factor_row_count=0,
            admitted_2017_anchor_count=0,
        ),
    )


def frozen_corporate_action_failure_evidence(
) -> tuple[CorporateActionFailureEvidence, ...]:
    """Freeze formal failures; candidates and corroboration are not quality rows."""

    return (
        CorporateActionFailureEvidence(
            evidence_id="SSE_CNINFO_ANNOUNCEMENT_DUAL_SOURCE_SAMPLE",
            manifest_sha256=CORPORATE_ACTION_FAILURE_MANIFEST_SHA256,
            target_security_count=3,
            expected_security_count=EXPECTED_FULL_TARGET_COUNT,
            candidate_or_corroboration_row_count=10,
            gp30_quality_row_count=0,
            gp43_quality_row_count=0,
            factor_eligible_event_count=0,
            ready=False,
            limitations=(
                "ONLY_3_OF_239_TARGET_SECURITIES_CAPTURED",
                "SSE_PDF_FETCH_RETURNED_NON_PDF_CHALLENGE_BYTES",
                "NO_INDEPENDENT_SZSE_SECOND_SOURCE",
                "CANDIDATE_ANNOUNCEMENTS_WERE_NOT_NORMALIZED_AS_QUALITY_EVENTS",
            ),
        ),
        CorporateActionFailureEvidence(
            evidence_id="SSE_STRUCTURED_CASH_DIVIDEND_CORROBORATION",
            manifest_sha256=SSE_DIVIDEND_CORROBORATION_MANIFEST_SHA256,
            target_security_count=2,
            expected_security_count=EXPECTED_FULL_TARGET_COUNT,
            candidate_or_corroboration_row_count=2,
            gp30_quality_row_count=0,
            gp43_quality_row_count=0,
            factor_eligible_event_count=0,
            ready=False,
            limitations=(
                "SSE_ONLY_AND_ONLY_2_OF_239_TARGET_SECURITIES_CAPTURED",
                "PUBLISHED_AT_UNAVAILABLE_FROM_SOURCE",
                "SOURCE_DOCUMENT_HASH_UNAVAILABLE_FROM_SOURCE",
                "CASH_DIVIDENDS_DO_NOT_PROVE_COMPLETE_ACTION_TYPE_COVERAGE",
            ),
        ),
    )


def frozen_factor_admission_requirements(
) -> tuple[FactorAdmissionRequirement, ...]:
    return (
        FactorAdmissionRequirement(
            requirement_id="DAILY_GRAIN_COMPLETENESS",
            grain="exchange,code,trade_date",
            required_evidence=(
                "exactly one adjusted row for every raw tradable bar and no "
                "adjusted row without a raw bar"
            ),
            hard_check="raw and adjusted daily key sets are identical",
            current_status="RAW_56_OF_239_FACTOR_0_OF_239",
            admitted_row_count=0,
        ),
        FactorAdmissionRequirement(
            requirement_id="POSITIVE_FINITE_FACTOR_AND_FRONT_OHLC",
            grain="exchange,code,trade_date",
            required_evidence="positive finite factor and front open/high/low/close",
            hard_check="all five numeric values are finite and greater than zero",
            current_status="MISSING",
            admitted_row_count=0,
        ),
        FactorAdmissionRequirement(
            requirement_id="RAW_FACTOR_FRONT_ARITHMETIC",
            grain="exchange,code,trade_date,ohlc_field",
            required_evidence="hash-bound raw OHLC, factor, and front-adjusted OHLC",
            hard_check=(
                "front_field equals raw_field times adjustment_factor with "
                "relative tolerance 1e-9 and absolute tolerance 1e-6"
            ),
            current_status="NOT_TESTABLE_WITHOUT_ADMITTED_FACTORS",
            admitted_row_count=0,
        ),
        FactorAdmissionRequirement(
            requirement_id="PRE_PARTITION_AND_2017_ANCHOR",
            grain="exchange,code,partition_year",
            required_evidence=(
                "a preceding anchor factor on the first row; securities active "
                "at the 2018 boundary require the last 2017 trading-day anchor"
            ),
            hard_check=(
                "anchor date precedes first partition trade date, factor is positive, "
                "and later rows contain no anchor fields"
            ),
            current_status="ZERO_ANCHORS_ADMITTED",
            admitted_row_count=0,
        ),
        FactorAdmissionRequirement(
            requirement_id="INDEPENDENT_GP30_GP43_EVENT_RECONCILIATION",
            grain="exchange,code,event_type,ex_date,ratio,cash_amount",
            required_evidence=(
                "two independently sourced event histories, each with point-in-time "
                "timestamps and a cold-replayed source document hash"
            ),
            hard_check="GP30 and GP43 reconciliation-key sets are exactly equal",
            current_status="GP30_ZERO_ROWS_GP43_ZERO_ROWS",
            admitted_row_count=0,
        ),
        FactorAdmissionRequirement(
            requirement_id="FACTOR_CHANGE_EVENT_SET_EQUALITY",
            grain="exchange,code,change_date",
            required_evidence=(
                "factor sequence beginning with the anchor plus reconciled GP30/GP43 events"
            ),
            hard_check=(
                "factor-change dates equal reconciled event ex-dates with factor "
                "comparison relative and absolute tolerance 1e-12"
            ),
            current_status="NOT_TESTABLE_WITH_ZERO_FACTOR_AND_EVENT_ROWS",
            admitted_row_count=0,
        ),
        FactorAdmissionRequirement(
            requirement_id="ROW_LEVEL_SOURCE_AND_DERIVATION_PROVENANCE",
            grain="exchange,code,trade_date",
            required_evidence=(
                "content-addressed source and derivation manifest binding raw bar, "
                "front bar, factor, anchor, and all causal corporate actions"
            ),
            hard_check="cold replay reproduces every value and dependency identity",
            current_status="MISSING",
            admitted_row_count=0,
        ),
    )


def frozen_authorized_source_requirements(
) -> tuple[AuthorizedSourceRequirement, ...]:
    return (
        AuthorizedSourceRequirement(
            source_id="SSE_DAILY_RAW_FRONT_FACTOR_ARCHIVE",
            exchange="SSE",
            target_security_count=99,
            minimum_capability=(
                "licensed or official daily raw OHLC, front OHLC, per-bar factor, "
                "methodology/version, and 2017 anchors; raw coverage must also close "
                "the current 43-security SSE gap"
            ),
            current_authorized_security_count=0,
            current_status="NO_ADMITTED_FACTOR_ARCHIVE",
            independence_requirement=(
                "factor provenance must be independent of price-gap inference"
            ),
        ),
        AuthorizedSourceRequirement(
            source_id="SZSE_DAILY_RAW_FRONT_FACTOR_ARCHIVE",
            exchange="SZSE",
            target_security_count=140,
            minimum_capability=(
                "licensed or official daily raw OHLC, front OHLC, per-bar factor, "
                "methodology/version, and 2017 anchors for all SZSE targets"
            ),
            current_authorized_security_count=0,
            current_status="NO_ADMITTED_RAW_OR_FACTOR_ARCHIVE",
            independence_requirement=(
                "factor provenance must be independent of price-gap inference"
            ),
        ),
        AuthorizedSourceRequirement(
            source_id="GP30_CORPORATE_ACTION_HISTORY",
            exchange="SSE_AND_SZSE",
            target_security_count=239,
            minimum_capability=(
                "complete event types and zero-event proof with event ID, ex-date, "
                "ratio, cash amount, published/effective timestamps, and document hash"
            ),
            current_authorized_security_count=0,
            current_status="ZERO_QUALITY_ROWS",
            independence_requirement="must not reuse the GP43 upstream or document copy",
        ),
        AuthorizedSourceRequirement(
            source_id="GP43_CORPORATE_ACTION_HISTORY",
            exchange="SSE_AND_SZSE",
            target_security_count=239,
            minimum_capability=(
                "complete event types and zero-event proof with event ID, ex-date, "
                "ratio, cash amount, published/effective timestamps, and document hash"
            ),
            current_authorized_security_count=0,
            current_status="ZERO_QUALITY_ROWS",
            independence_requirement="must not reuse the GP30 upstream or document copy",
        ),
    )


def build_frozen_adjusted_bar_factor_source_capability_assessment(
) -> AdjustedBarFactorSourceCapabilityAssessment:
    coverage = frozen_exchange_coverage()
    failure_evidence = frozen_corporate_action_failure_evidence()
    requirements = frozen_factor_admission_requirements()
    authorizations = frozen_authorized_source_requirements()
    _validate_frozen_inputs(coverage, failure_evidence, requirements, authorizations)
    logical_payload = {
        "protocol_version": PROTOCOL_VERSION,
        "exchange_coverage": [item.to_dict() for item in coverage],
        "corporate_action_failure_evidence": [
            item.to_dict() for item in failure_evidence
        ],
        "admission_requirements": [item.to_dict() for item in requirements],
        "authorized_source_requirements": [
            item.to_dict() for item in authorizations
        ],
        "minimum_authorized_sources": list(MINIMUM_AUTHORIZED_SOURCES),
        "minimum_external_evidence": list(MINIMUM_EXTERNAL_EVIDENCE),
        "prohibited_inferences": list(PROHIBITED_INFERENCES),
    }
    artifact = AdjustedBarFactorSourceCapabilityAssessment(
        exchange_coverage=coverage,
        corporate_action_failure_evidence=failure_evidence,
        admission_requirements=requirements,
        authorized_source_requirements=authorizations,
        logical_content_sha256=_sha256(_canonical_json_bytes(logical_payload)),
        _seal=_ASSESSMENT_SEAL,
    )
    if (
        artifact.ready
        or artifact.quality_rows_emitted != 0
        or artifact.training_allowed
        or artifact.trading_allowed
        or artifact.promotion_allowed
    ):
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "factor-source assessment attempted to emit or authorize data"
        )
    return artifact


def replay_frozen_adjusted_bar_factor_source_capability_assessment(
    value: Mapping[str, Any],
) -> AdjustedBarFactorSourceCapabilityAssessment:
    artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
    if dict(value) != artifact.to_dict():
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "adjusted-bar factor source assessment did not replay exactly"
        )
    return artifact


def _validate_frozen_inputs(
    coverage: Sequence[ExchangeCoverageObservation],
    failure_evidence: Sequence[CorporateActionFailureEvidence],
    requirements: Sequence[FactorAdmissionRequirement],
    authorizations: Sequence[AuthorizedSourceRequirement],
) -> None:
    if {item.exchange: item.target_security_count for item in coverage} != EXPECTED_EXCHANGE_COUNTS:
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "frozen factor target scope changed"
        )
    if sum(item.target_security_count for item in coverage) != EXPECTED_FULL_TARGET_COUNT:
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "factor exchange counts do not reconcile"
        )
    expected_raw = {"SSE": 56, "SZSE": 0}
    for item in coverage:
        if (
            item.official_raw_security_count != expected_raw[item.exchange]
            or item.raw_security_missing_count
            != item.target_security_count - item.official_raw_security_count
            or item.admitted_factor_security_count != 0
            or item.admitted_factor_row_count != 0
            or item.admitted_2017_anchor_count != 0
        ):
            raise AdjustedBarFactorSourceAssessmentBlockedError(
                "frozen partial raw/factor coverage was overclaimed"
            )
        if item.exchange == "SSE":
            if (
                not item.raw_source_index_present
                or item.raw_source_index_sha256 != SSE_RAW_SOURCE_INDEX_SHA256
            ):
                raise AdjustedBarFactorSourceAssessmentBlockedError(
                    "SSE partial raw coverage lost its source-index binding"
                )
        elif item.raw_source_index_present or item.raw_source_index_sha256 is not None:
            raise AdjustedBarFactorSourceAssessmentBlockedError(
                "SZSE raw coverage was overclaimed"
            )

    expected_failure_ids = {
        "SSE_CNINFO_ANNOUNCEMENT_DUAL_SOURCE_SAMPLE",
        "SSE_STRUCTURED_CASH_DIVIDEND_CORROBORATION",
    }
    if {item.evidence_id for item in failure_evidence} != expected_failure_ids:
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "corporate-action failure evidence changed"
        )
    for item in failure_evidence:
        _strict_sha256(item.manifest_sha256, "failure manifest")
        if (
            item.ready
            or item.expected_security_count != EXPECTED_FULL_TARGET_COUNT
            or item.target_security_count >= EXPECTED_FULL_TARGET_COUNT
            or item.gp30_quality_row_count != 0
            or item.gp43_quality_row_count != 0
            or item.factor_eligible_event_count != 0
            or not item.limitations
        ):
            raise AdjustedBarFactorSourceAssessmentBlockedError(
                "corporate-action failure evidence was overclaimed"
            )

    expected_requirement_ids = {
        "DAILY_GRAIN_COMPLETENESS",
        "POSITIVE_FINITE_FACTOR_AND_FRONT_OHLC",
        "RAW_FACTOR_FRONT_ARITHMETIC",
        "PRE_PARTITION_AND_2017_ANCHOR",
        "INDEPENDENT_GP30_GP43_EVENT_RECONCILIATION",
        "FACTOR_CHANGE_EVENT_SET_EQUALITY",
        "ROW_LEVEL_SOURCE_AND_DERIVATION_PROVENANCE",
    }
    if {item.requirement_id for item in requirements} != expected_requirement_ids:
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "factor admission requirement set changed"
        )
    if any(item.admitted_row_count != 0 for item in requirements):
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "an incomplete factor requirement admitted rows"
        )

    expected_source_ids = {
        "SSE_DAILY_RAW_FRONT_FACTOR_ARCHIVE",
        "SZSE_DAILY_RAW_FRONT_FACTOR_ARCHIVE",
        "GP30_CORPORATE_ACTION_HISTORY",
        "GP43_CORPORATE_ACTION_HISTORY",
    }
    if {item.source_id for item in authorizations} != expected_source_ids:
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "minimum authorized source set changed"
        )
    if any(
        item.current_authorized_security_count != 0
        or item.target_security_count <= 0
        or not item.minimum_capability
        or not item.independence_requirement
        for item in authorizations
    ):
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            "authorization coverage was overclaimed or underspecified"
        )
    _strict_sha256(SSE_RAW_SOURCE_INDEX_SHA256, "SSE raw source index")
    if tuple(BLOCKERS) != BLOCKERS or len(set(BLOCKERS)) != len(BLOCKERS):
        raise AdjustedBarFactorSourceAssessmentBlockedError("factor blockers changed")


def _strict_sha256(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if _SHA256.fullmatch(digest) is None:
        raise AdjustedBarFactorSourceAssessmentBlockedError(
            f"invalid {label} SHA-256"
        )
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
    "ADJUSTED_BAR_FACTOR_SCHEMA",
    "AdjustedBarFactorSourceAssessmentBlockedError",
    "AdjustedBarFactorSourceCapabilityAssessment",
    "BLOCKERS",
    "EXPECTED_EXCHANGE_COUNTS",
    "EXPECTED_FULL_TARGET_COUNT",
    "MINIMUM_AUTHORIZED_SOURCES",
    "MINIMUM_EXTERNAL_EVIDENCE",
    "PROHIBITED_INFERENCES",
    "SOURCE_STATUS",
    "build_frozen_adjusted_bar_factor_source_capability_assessment",
    "replay_frozen_adjusted_bar_factor_source_capability_assessment",
]
