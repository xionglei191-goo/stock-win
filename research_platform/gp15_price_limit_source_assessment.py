from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


PROTOCOL_VERSION = "gp15-official-price-limit-source-capability-assessment-v1"
SOURCE_STATUS = "OFFICIAL_RULE_EVIDENCE_PARTIAL_GP15_UNADMITTED"
SOURCE_SCOPE = "FROZEN_239_DELISTED_SECURITY_SCOPE"
AUDIT_START = "2018-01-01"
AUDIT_END = "2023-12-31"
EXPECTED_FULL_TARGET_COUNT = 239
EXPECTED_EXCHANGE_COUNTS = {"SSE": 99, "SZSE": 140}

GP15_SCHEMA = (
    "exchange",
    "code",
    "trade_date",
    "limit_up",
    "limit_down",
    "published_at",
    "effective_at",
    "source_document_hash",
)

BLOCK_FULL_SCOPE = "FULL_239_SECURITY_DAILY_LIMIT_SCOPE_NOT_CLOSED"
BLOCK_REFERENCE_PRICE = "OFFICIAL_PREVIOUS_CLOSE_OR_EX_RIGHT_REFERENCE_MISSING"
BLOCK_RULE_MATRIX = "VERSIONED_BOARD_DATE_PRICE_LIMIT_RULE_MATRIX_INCOMPLETE"
BLOCK_ST_STATUS = "POINT_IN_TIME_ST_STATUS_INTERVALS_MISSING"
BLOCK_IPO_WINDOW = "IPO_RELISTING_NO_LIMIT_WINDOWS_INCOMPLETE"
BLOCK_DELISTING_RULE = "DELISTING_PERIOD_SPECIAL_RULES_INCOMPLETE"
BLOCK_ROUNDING = "PRICE_TICK_AND_ROUNDING_RULE_HISTORY_INCOMPLETE"
BLOCK_PROVENANCE = "ROW_LEVEL_PUBLISHED_EFFECTIVE_HASH_PROVENANCE_MISSING"
BLOCK_NO_LIMIT_SCHEMA = "NUMERIC_GP15_SCHEMA_CANNOT_REPRESENT_NO_LIMIT_SESSION"

BLOCKERS = (
    BLOCK_FULL_SCOPE,
    BLOCK_REFERENCE_PRICE,
    BLOCK_RULE_MATRIX,
    BLOCK_ST_STATUS,
    BLOCK_IPO_WINDOW,
    BLOCK_DELISTING_RULE,
    BLOCK_ROUNDING,
    BLOCK_PROVENANCE,
    BLOCK_NO_LIMIT_SCHEMA,
)

PROHIBITED_INFERENCES = (
    "CURRENT_SESSION_HIGH_OR_LOW_AS_LIMIT_UP_OR_LIMIT_DOWN",
    "CURRENT_SESSION_OHLC_OR_RETURN_AS_THE_LIMIT_REGIME",
    "CURRENT_NAME_OR_POST_EVENT_NAME_BACKFILL_AS_HISTORICAL_ST_STATUS",
    "CODE_PREFIX_ALONE_AS_A_COMPLETE_BOARD_AND_RULE_VERSION_HISTORY",
    "FUTURE_CORPORATE_ACTION_OR_FUTURE_STATUS_BACKFILL",
    "TDX_GP15_OR_VENDOR_LOCK_STATE_AS_OFFICIAL_NUMERIC_PRICE_LIMITS",
    "ARBITRARY_FLOAT_ROUNDING_WITHOUT_THE_EFFECTIVE_EXCHANGE_TICK_RULE",
    "SYNTHETIC_WIDE_BOUNDS_FOR_A_NO_PRICE_LIMIT_SESSION",
)

MINIMUM_EXTERNAL_EVIDENCE = (
    "OFFICIAL_SESSION_PREVIOUS_CLOSE_OR_EX_RIGHT_REFERENCE_PRICE_FOR_EACH_REQUIRED_SECURITY_DATE",
    "OFFICIAL_EFFECTIVE_DATED_CORPORATE_ACTIONS_AND_REFERENCE_PRICE_FORMULA_INPUTS",
    "COLD_REPLAYED_VERSIONED_SSE_AND_SZSE_RULES_WITH_COMPLETE_EFFECTIVE_INTERVALS",
    "OFFICIAL_POINT_IN_TIME_ST_AND_RISK_WARNING_INTERVALS_FOR_ALL_239_SECURITIES",
    "OFFICIAL_IPO_RELISTING_AND_OTHER_NO_PRICE_LIMIT_WINDOWS_FOR_ALL_239_SECURITIES",
    "OFFICIAL_DELISTING_PERIOD_START_END_AND_BOARD_SPECIFIC_LIMIT_RULES",
    "OFFICIAL_PRICE_TICK_LOW_PRICE_EXCEPTION_AND_ROUNDING_RULE_HISTORY",
    "CONTENT_ADDRESSED_DERIVATION_MANIFEST_BINDING_ALL_INPUT_HASHES_PER_DAILY_ROW",
    "PUBLISHED_AT_AND_EFFECTIVE_AT_POLICY_PROVING_EACH_ROW_WAS_AVAILABLE_BY_TRADE_DATE",
    "EXPLICIT_NO_PRICE_LIMIT_REPRESENTATION_ACCEPTED_BY_THE_GP15_QUALITY_CONTRACT",
    "COMPLETE_OFFICIAL_CALENDAR_AND_DAILY_SOURCE_PARTITIONS_FOR_ALL_239_SECURITIES",
)

_ASSESSMENT_SEAL = object()
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OFFICIAL_HOSTS = {
    "big5.sse.com.cn",
    "edu.sse.com.cn",
    "sse.com.cn",
    "star.sse.com.cn",
    "static.cninfo.com.cn",
    "static.sse.com.cn",
    "www.cninfo.com.cn",
    "www.sse.com.cn",
    "www.szse.cn",
}


class GP15PriceLimitSourceAssessmentBlockedError(RuntimeError):
    """The frozen capability assessment was changed or overclaimed."""


@dataclass(frozen=True)
class FrozenCoverageObservation:
    master_snapshot_id: str
    master_content_sha256: str
    quality_audit_report_sha256: str
    raw_bar_source_index_sha256: str
    target_security_count: int
    target_exchange_counts: tuple[tuple[str, int], ...]
    observed_official_raw_security_count: int
    observed_raw_session_count: int
    observed_gp15_limit_missing_count: int
    gp15_source_index_present: bool
    admitted_gp15_row_count: int

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["target_exchange_counts"] = dict(self.target_exchange_counts)
        return value


@dataclass(frozen=True)
class OfficialRuleEvidence:
    evidence_id: str
    exchange: str
    code: str | None
    board: str
    published_at: str | None
    effective_from: str | None
    effective_to_exclusive: str | None
    source_url: str
    source_document_sha256: str | None
    raw_document_cold_replayed: bool
    point_in_time_fact_eligible: bool
    complete_for_2018_2023_scope: bool
    observations: tuple[str, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["observations"] = list(self.observations)
        value["limitations"] = list(self.limitations)
        return value


@dataclass(frozen=True)
class DailyLimitInputRequirement:
    requirement_id: str
    grain: str
    required_value: str
    current_status: str
    admitted_daily_row_count: int
    prohibited_substitutes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["prohibited_substitutes"] = list(self.prohibited_substitutes)
        return value


@dataclass(frozen=True)
class NoLimitSchemaConflict:
    conflict_id: str
    exchange: str
    code: str
    board: str
    window: str
    official_rule_observation: str
    current_contract_conflict: str
    required_resolution: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GP15PriceLimitSourceCapabilityAssessment:
    coverage_observation: FrozenCoverageObservation
    rule_evidence: tuple[OfficialRuleEvidence, ...]
    input_requirements: tuple[DailyLimitInputRequirement, ...]
    no_limit_schema_conflicts: tuple[NoLimitSchemaConflict, ...]
    logical_content_sha256: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ASSESSMENT_SEAL:
            raise TypeError("GP15 assessments must be built by the frozen builder")

    @property
    def ready(self) -> bool:
        return False

    @property
    def quality_rows_emitted(self) -> int:
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
            "schema": list(GP15_SCHEMA),
            "source_authority_by_exchange": {
                "SSE": "SSE_OFFICIAL_DAILY_STATUS",
                "SZSE": "SZSE_AUTHORIZED_DAILY_STATUS",
            },
            "official_rule_references_only": True,
            "caller_ready_attestation_allowed": False,
            "quality_dataset_eligibility": False,
            "gp15_rows_may_be_emitted": False,
            "training_allowed": False,
            "label_generation_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
            "current_day_ohlc_inference_allowed": False,
            "post_event_backfill_allowed": False,
        }

    @property
    def coverage(self) -> dict[str, Any]:
        frozen = self.coverage_observation
        target_counts = dict(frozen.target_exchange_counts)
        return {
            "target_security_count": frozen.target_security_count,
            "target_exchange_counts": target_counts,
            "observed_official_raw_security_count": (
                frozen.observed_official_raw_security_count
            ),
            "observed_raw_session_count": frozen.observed_raw_session_count,
            "observed_raw_session_gp15_gap_count": (
                frozen.observed_gp15_limit_missing_count
            ),
            "observed_security_coverage_ratio": (
                frozen.observed_official_raw_security_count
                / frozen.target_security_count
            ),
            "full_scope_required_session_count": None,
            "full_scope_daily_coverage_closed": False,
            "gp15_source_index_present": frozen.gp15_source_index_present,
            "official_rule_reference_count": len(self.rule_evidence),
            "cold_replayed_rule_evidence_count": sum(
                item.raw_document_cold_replayed for item in self.rule_evidence
            ),
            "complete_rule_evidence_count": sum(
                item.complete_for_2018_2023_scope for item in self.rule_evidence
            ),
            "admitted_reference_price_row_count": 0,
            "admitted_st_status_interval_count": 0,
            "identified_no_limit_schema_conflict_count": len(
                self.no_limit_schema_conflicts
            ),
            "quality_rows_emitted": 0,
            "full_scope_security_count_admitted": 0,
            "blockers": list(BLOCKERS),
        }

    @property
    def minimum_external_evidence(self) -> tuple[str, ...]:
        return MINIMUM_EXTERNAL_EVIDENCE

    @property
    def prohibited_inferences(self) -> tuple[str, ...]:
        return PROHIBITED_INFERENCES

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "ready": False,
            "quality_rows_emitted": 0,
            "training_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
            "coverage_observation": self.coverage_observation.to_dict(),
            "rule_evidence": [item.to_dict() for item in self.rule_evidence],
            "input_requirements": [
                item.to_dict() for item in self.input_requirements
            ],
            "no_limit_schema_conflicts": [
                item.to_dict() for item in self.no_limit_schema_conflicts
            ],
            "coverage": self.coverage,
            "minimum_external_evidence": list(self.minimum_external_evidence),
            "prohibited_inferences": list(self.prohibited_inferences),
            "source_contract": self.source_contract,
            "logical_content_sha256": self.logical_content_sha256,
        }


def frozen_coverage_observation() -> FrozenCoverageObservation:
    """Freeze source coverage without treating raw OHLC as GP15 evidence."""

    return FrozenCoverageObservation(
        master_snapshot_id=(
            "1ce6cc99a95e88b243bee74fd5e30638d33577aeb07b700be32891887591fe37"
        ),
        master_content_sha256=(
            "c8842351ef2e27ba7b93ebb4c0a2177f17a9ccd3baced6c43eeda57a03a3b1fd"
        ),
        quality_audit_report_sha256=(
            "2ee748f847fa4777f69557c7fb23c3ce88513b92ecc07ef63ab4156accc1cc0a"
        ),
        raw_bar_source_index_sha256=(
            "4444e219c7aa9f7db0fa238e1f1107d0c43a6b5e064430be0d55d3156d123dea"
        ),
        target_security_count=239,
        target_exchange_counts=(("SSE", 99), ("SZSE", 140)),
        observed_official_raw_security_count=56,
        observed_raw_session_count=46_394,
        observed_gp15_limit_missing_count=46_394,
        gp15_source_index_present=False,
        admitted_gp15_row_count=0,
    )


def frozen_official_rule_evidence() -> tuple[OfficialRuleEvidence, ...]:
    """Return official rule observations, never derived daily limit rows.

    A rule percentage is not a numeric daily bound.  A bound additionally needs
    the applicable session reference price, status, exception window, tick rule,
    and point-in-time provenance.
    """

    return (
        OfficialRuleEvidence(
            evidence_id="SSE_MAIN_RISK_WARNING_AND_DELISTING_RULE_REFERENCE",
            exchange="SSE",
            code=None,
            board="SSE_MAIN",
            published_at=None,
            effective_from=None,
            effective_to_exclusive=None,
            source_url=(
                "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/"
                "rules/c/c_20230421_5720459.shtml"
            ),
            source_document_sha256=None,
            raw_document_cold_replayed=False,
            point_in_time_fact_eligible=False,
            complete_for_2018_2023_scope=False,
            observations=(
                "RISK_WARNING_SHARES_USE_A_5_PERCENT_LIMIT_UNDER_THE_REFERENCED_RULE",
                "DELISTING_PERIOD_SHARES_USE_A_10_PERCENT_LIMIT_UNDER_THE_REFERENCED_RULE",
                "LOW_PREVIOUS_CLOSE_ABSOLUTE_PRICE_EXCEPTIONS_EXIST",
                "THE_FORMULA_USES_THE_PREVIOUS_CLOSE_AS_ITS_BASE",
            ),
            limitations=(
                "HISTORICAL_VERSION_CHAIN_AND_EFFECTIVE_INTERVALS_NOT_COLD_REPLAYED",
                "REFERENCE_HAS_NO_LOCAL_CONTENT_HASH_BINDING",
            ),
        ),
        OfficialRuleEvidence(
            evidence_id="SSE_2018_TRADING_RULE_EX_RIGHT_REFERENCE",
            exchange="SSE",
            code=None,
            board="ALL_SSE_A_SHARE_BOARDS",
            published_at="2018-08-06T00:00:00+08:00",
            effective_from="2018-08-20",
            effective_to_exclusive="2020-03-13",
            source_url=(
                "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/"
                "rules/c/c_20210531_5478087.shtml"
            ),
            source_document_sha256=None,
            raw_document_cold_replayed=False,
            point_in_time_fact_eligible=False,
            complete_for_2018_2023_scope=False,
            observations=(
                "EX_RIGHT_OR_EX_DIVIDEND_DAY_USES_THE_EX_RIGHT_REFERENCE_PRICE",
                "ISSUER_SPECIFIC_REFERENCE_PRICE_FORMULA_ADJUSTMENTS_MAY_BE_PUBLISHED",
            ),
            limitations=(
                "PRE_2018_08_20_AND_POST_2020_03_13_RULE_BYTES_NOT_BOUND",
                "REFERENCE_HAS_NO_LOCAL_CONTENT_HASH_BINDING",
            ),
        ),
        OfficialRuleEvidence(
            evidence_id="SSE_STAR_BOARD_SPECIAL_TRADING_RULE_REFERENCE",
            exchange="SSE",
            code=None,
            board="STAR",
            published_at=None,
            effective_from=None,
            effective_to_exclusive=None,
            source_url=(
                "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/"
                "rules/c/10118601/files/f6fc4a1d4c1f469183a013c4dc36a535.pdf"
            ),
            source_document_sha256=None,
            raw_document_cold_replayed=False,
            point_in_time_fact_eligible=False,
            complete_for_2018_2023_scope=False,
            observations=(
                "STAR_SHARES_USE_A_20_PERCENT_LIMIT_WHEN_A_LIMIT_APPLIES",
                "STAR_IPO_SHARES_HAVE_NO_PRICE_LIMIT_FOR_THE_FIRST_5_TRADING_DAYS",
                "THE_REFERENCED_STAR_FORMULA_USES_THE_PREVIOUS_CLOSE",
            ),
            limitations=(
                "RULE_VERSION_EFFECTIVE_INTERVALS_NOT_COLD_REPLAYED",
                "NO_PRICE_LIMIT_SESSIONS_HAVE_NO_NUMERIC_GP15_REPRESENTATION",
            ),
        ),
        OfficialRuleEvidence(
            evidence_id="SSE_600432_DELISTING_2018_EXCHANGE_QA",
            exchange="SSE",
            code="600432.SH",
            board="SSE_MAIN",
            published_at="2018-05-22T00:00:00+08:00",
            effective_from="2018-05-30",
            effective_to_exclusive="2018-07-12",
            source_url=(
                "https://www.sse.com.cn/aboutus/mediacenter/hotandd/"
                "c/c_20180522_4559300.shtml"
            ),
            source_document_sha256=None,
            raw_document_cold_replayed=False,
            point_in_time_fact_eligible=False,
            complete_for_2018_2023_scope=False,
            observations=(
                "600432_SH_DELISTING_PERIOD_STARTED_2018_05_30",
                "600432_SH_DELISTING_PERIOD_LIMIT_RATIO_WAS_10_PERCENT_EXCEPT_SPECIAL_CASES",
            ),
            limitations=(
                "EXCHANGE_PAGE_NOT_COLD_REPLAYED_OR_CONTENT_HASH_BOUND",
                "NO_DAILY_REFERENCE_PRICE_OR_ROUNDED_LIMIT_PRICE_IS_PUBLISHED",
            ),
        ),
        OfficialRuleEvidence(
            evidence_id="CNINFO_000511_DELISTING_NOTICE_20180529",
            exchange="SZSE",
            code="000511.SZ",
            board="SZSE_MAIN",
            published_at="2018-05-29T00:00:00+08:00",
            effective_from="2018-06-05",
            effective_to_exclusive="2018-07-18",
            source_url=(
                "https://static.cninfo.com.cn/finalpage/2018-05-29/"
                "1205010494.PDF"
            ),
            source_document_sha256=(
                "41241acdcad3417ab13022c6aad54757b0cc74f0254bc7a3c1b154a7d42fe0a1"
            ),
            raw_document_cold_replayed=True,
            point_in_time_fact_eligible=True,
            complete_for_2018_2023_scope=False,
            observations=(
                "000511_SZ_DELISTING_PERIOD_STARTED_2018_06_05",
                "000511_SZ_DELISTING_PERIOD_LIMIT_RATIO_WAS_10_PERCENT",
            ),
            limitations=(
                "EVIDENCE_COVERS_ONLY_ONE_SECURITY_AND_ONE_EVENT_WINDOW",
                "NO_DAILY_REFERENCE_PRICE_OR_ROUNDED_LIMIT_PRICE_IS_PUBLISHED",
            ),
        ),
        OfficialRuleEvidence(
            evidence_id="SZSE_CHINEXT_2020_LIMIT_TRANSITION_NOTICE",
            exchange="SZSE",
            code=None,
            board="CHINEXT",
            published_at="2020-07-10T00:00:00+08:00",
            effective_from="2020-08-24",
            effective_to_exclusive=None,
            source_url=(
                "https://www.szse.cn/disclosure/notice/general/"
                "t20200710_579459.html"
            ),
            source_document_sha256=None,
            raw_document_cold_replayed=False,
            point_in_time_fact_eligible=False,
            complete_for_2018_2023_scope=False,
            observations=(
                "POST_REFORM_CHINEXT_RISK_WARNING_AND_DELISTING_SHARES_USE_A_20_PERCENT_LIMIT",
                "A_TRANSITION_RULE_APPLIES_TO_SHARES_ALREADY_IN_DELISTING_BEFORE_EFFECTIVE_DATE",
            ),
            limitations=(
                "PRE_REFORM_AND_POST_REFORM_RULE_DOCUMENT_CHAIN_NOT_COLD_REPLAYED",
                "REFERENCE_HAS_NO_LOCAL_CONTENT_HASH_BINDING",
            ),
        ),
        OfficialRuleEvidence(
            evidence_id="SSE_688086_DELISTING_2023_EXCHANGE_DECISION",
            exchange="SSE",
            code="688086.SH",
            board="STAR",
            published_at="2023-05-31T00:00:00+08:00",
            effective_from="2023-06-08",
            effective_to_exclusive="2023-07-01",
            source_url=(
                "https://www.sse.com.cn/disclosure/announcement/listing/"
                "stock/c/c_20230531_89787013.shtml"
            ),
            source_document_sha256=None,
            raw_document_cold_replayed=False,
            point_in_time_fact_eligible=False,
            complete_for_2018_2023_scope=False,
            observations=(
                "688086_SH_DELISTING_FIRST_SESSION_2023_06_08_HAD_NO_PRICE_LIMIT",
                "688086_SH_OTHER_DELISTING_SESSIONS_USED_A_20_PERCENT_LIMIT",
            ),
            limitations=(
                "EXCHANGE_PAGE_NOT_COLD_REPLAYED_OR_CONTENT_HASH_BOUND",
                "NO_LIMIT_FIRST_SESSION_CONFLICTS_WITH_THE_NUMERIC_GP15_SCHEMA",
            ),
        ),
    )


def frozen_daily_limit_input_requirements(
) -> tuple[DailyLimitInputRequirement, ...]:
    return (
        DailyLimitInputRequirement(
            requirement_id="OFFICIAL_SESSION_REFERENCE_PRICE",
            grain="exchange,code,trade_date",
            required_value=(
                "previous close, or the exchange-published ex-right/ex-dividend "
                "reference price when applicable"
            ),
            current_status="PARTIAL_RAW_BARS_EXIST_BUT_NO_FULL_REFERENCE_PRICE_LEDGER",
            admitted_daily_row_count=0,
            prohibited_substitutes=(
                "current-session open/high/low/close",
                "a later adjusted close",
            ),
        ),
        DailyLimitInputRequirement(
            requirement_id="OFFICIAL_CORPORATE_ACTION_REFERENCE_INPUTS",
            grain="exchange,code,ex_date,event_id",
            required_value=(
                "cash distribution, rights issue, share change, issuer-specific "
                "formula, and the resulting reference price"
            ),
            current_status="MISSING",
            admitted_daily_row_count=0,
            prohibited_substitutes=(
                "future adjustment factor",
                "current-day OHLC discontinuity",
            ),
        ),
        DailyLimitInputRequirement(
            requirement_id="VERSIONED_BOARD_DATE_RULE_REGIME",
            grain="exchange,board,effective_from,effective_to_exclusive",
            required_value=(
                "normal, risk-warning, no-limit, low-price exception, and "
                "transition rules with exact effective intervals"
            ),
            current_status="OFFICIAL_REFERENCES_ONLY_VERSION_CHAIN_INCOMPLETE",
            admitted_daily_row_count=0,
            prohibited_substitutes=("code prefix alone", "current exchange rules"),
        ),
        DailyLimitInputRequirement(
            requirement_id="POINT_IN_TIME_ST_STATUS",
            grain="exchange,code,valid_from,valid_to",
            required_value=(
                "official NORMAL, ST, STAR_ST, or other effective risk-warning state"
            ),
            current_status="MISSING_FOR_FULL_SCOPE",
            admitted_daily_row_count=0,
            prohibited_substitutes=(
                "current security name",
                "a later delisting announcement title",
            ),
        ),
        DailyLimitInputRequirement(
            requirement_id="IPO_RELISTING_NO_LIMIT_WINDOW",
            grain="exchange,code,trade_date",
            required_value=(
                "official listing/relisting event and counted open-session window"
            ),
            current_status="INCOMPLETE_AND_CURRENT_GP15_SCHEMA_HAS_NO_NO_LIMIT_STATE",
            admitted_daily_row_count=0,
            prohibited_substitutes=(
                "calendar-day count",
                "synthetic high and low numeric bounds",
            ),
        ),
        DailyLimitInputRequirement(
            requirement_id="DELISTING_PERIOD_SPECIAL_RULE",
            grain="exchange,code,trade_date",
            required_value=(
                "official delisting-period start/end, first-session exception, "
                "board-specific ratio, and transition rule"
            ),
            current_status="THREE_SAMPLE_EVENTS_ONLY_FULL_SCOPE_INCOMPLETE",
            admitted_daily_row_count=0,
            prohibited_substitutes=(
                "name containing delisting text",
                "post-period delisting date alone",
            ),
        ),
        DailyLimitInputRequirement(
            requirement_id="PRICE_TICK_AND_ROUNDING_RULE",
            grain="exchange,instrument_type,effective_from,effective_to_exclusive",
            required_value=(
                "minimum price tick, rounding method, and low-reference-price exceptions"
            ),
            current_status="VERSION_HISTORY_INCOMPLETE",
            admitted_daily_row_count=0,
            prohibited_substitutes=("binary floating-point round", "fixed 0.01 guess"),
        ),
        DailyLimitInputRequirement(
            requirement_id="ROW_LEVEL_POINT_IN_TIME_PROVENANCE",
            grain="exchange,code,trade_date",
            required_value=(
                "published_at, effective_at, and a content-addressed derivation "
                "manifest covering every official rule and price/status input"
            ),
            current_status="MISSING",
            admitted_daily_row_count=0,
            prohibited_substitutes=(
                "retrieval timestamp as publication timestamp",
                "one unbound rule URL for a multi-input derived row",
            ),
        ),
    )


def frozen_no_limit_schema_conflicts() -> tuple[NoLimitSchemaConflict, ...]:
    conflict = (
        "the current GP15 contract requires positive numeric limit_up and "
        "limit_down with limit_up greater than limit_down on every raw session"
    )
    resolution = (
        "add an explicit NO_PRICE_LIMIT session state to the quality contract "
        "and define its daily coverage rule before any GP15 rows are admitted"
    )
    return (
        NoLimitSchemaConflict(
            conflict_id="STAR_IPO_FIRST_FIVE_SESSIONS",
            exchange="SSE",
            code="688086.SH",
            board="STAR",
            window="from 2020-02-26 through the fifth official open session",
            official_rule_observation=(
                "STAR IPO shares have no price limit for their first five trading days"
            ),
            current_contract_conflict=conflict,
            required_resolution=resolution,
        ),
        NoLimitSchemaConflict(
            conflict_id="STAR_DELISTING_FIRST_SESSION",
            exchange="SSE",
            code="688086.SH",
            board="STAR",
            window="2023-06-08",
            official_rule_observation=(
                "the official termination decision states that the first "
                "delisting-period session had no price limit"
            ),
            current_contract_conflict=conflict,
            required_resolution=resolution,
        ),
    )


def build_frozen_gp15_source_capability_assessment(
) -> GP15PriceLimitSourceCapabilityAssessment:
    coverage = frozen_coverage_observation()
    rule_evidence = frozen_official_rule_evidence()
    requirements = frozen_daily_limit_input_requirements()
    conflicts = frozen_no_limit_schema_conflicts()
    _validate_frozen_inputs(coverage, rule_evidence, requirements, conflicts)
    logical_payload = {
        "protocol_version": PROTOCOL_VERSION,
        "coverage_observation": coverage.to_dict(),
        "rule_evidence": [item.to_dict() for item in rule_evidence],
        "input_requirements": [item.to_dict() for item in requirements],
        "no_limit_schema_conflicts": [item.to_dict() for item in conflicts],
        "minimum_external_evidence": list(MINIMUM_EXTERNAL_EVIDENCE),
        "prohibited_inferences": list(PROHIBITED_INFERENCES),
    }
    logical_hash = _sha256(_canonical_json_bytes(logical_payload))
    artifact = GP15PriceLimitSourceCapabilityAssessment(
        coverage_observation=coverage,
        rule_evidence=rule_evidence,
        input_requirements=requirements,
        no_limit_schema_conflicts=conflicts,
        logical_content_sha256=logical_hash,
        _seal=_ASSESSMENT_SEAL,
    )
    if artifact.ready or artifact.quality_rows_emitted != 0:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "source assessment attempted to emit admissible GP15 data"
        )
    return artifact


def replay_frozen_gp15_source_capability_assessment(
    value: Mapping[str, Any],
) -> GP15PriceLimitSourceCapabilityAssessment:
    artifact = build_frozen_gp15_source_capability_assessment()
    if dict(value) != artifact.to_dict():
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "GP15 source-capability assessment did not replay exactly"
        )
    return artifact


def _validate_frozen_inputs(
    coverage: FrozenCoverageObservation,
    rule_evidence: Sequence[OfficialRuleEvidence],
    requirements: Sequence[DailyLimitInputRequirement],
    conflicts: Sequence[NoLimitSchemaConflict],
) -> None:
    if coverage.target_security_count != EXPECTED_FULL_TARGET_COUNT:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "frozen GP15 target count changed"
        )
    if dict(coverage.target_exchange_counts) != EXPECTED_EXCHANGE_COUNTS:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "frozen GP15 exchange counts changed"
        )
    if sum(dict(coverage.target_exchange_counts).values()) != EXPECTED_FULL_TARGET_COUNT:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "frozen GP15 exchange counts do not reconcile"
        )
    for value in (
        coverage.master_snapshot_id,
        coverage.master_content_sha256,
        coverage.quality_audit_report_sha256,
        coverage.raw_bar_source_index_sha256,
    ):
        _sha256_text(value, "coverage evidence")
    if (
        coverage.observed_official_raw_security_count <= 0
        or coverage.observed_official_raw_security_count
        >= coverage.target_security_count
        or coverage.observed_raw_session_count <= 0
        or coverage.observed_gp15_limit_missing_count
        != coverage.observed_raw_session_count
        or coverage.gp15_source_index_present
        or coverage.admitted_gp15_row_count != 0
    ):
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "frozen partial GP15 coverage was overclaimed"
        )

    expected_codes = {"000511.SZ", "600432.SH", "688086.SH"}
    observed_codes = {item.code for item in rule_evidence if item.code is not None}
    if not expected_codes.issubset(observed_codes):
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "frozen official sample evidence changed"
        )
    if not rule_evidence or any(item.complete_for_2018_2023_scope for item in rule_evidence):
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "rule references cannot claim complete 2018-2023 coverage"
        )
    for item in rule_evidence:
        _validate_rule_evidence(item)

    expected_requirements = {
        "OFFICIAL_SESSION_REFERENCE_PRICE",
        "OFFICIAL_CORPORATE_ACTION_REFERENCE_INPUTS",
        "VERSIONED_BOARD_DATE_RULE_REGIME",
        "POINT_IN_TIME_ST_STATUS",
        "IPO_RELISTING_NO_LIMIT_WINDOW",
        "DELISTING_PERIOD_SPECIAL_RULE",
        "PRICE_TICK_AND_ROUNDING_RULE",
        "ROW_LEVEL_POINT_IN_TIME_PROVENANCE",
    }
    if {item.requirement_id for item in requirements} != expected_requirements:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "daily limit dependency set changed"
        )
    if any(item.admitted_daily_row_count != 0 for item in requirements):
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "an incomplete GP15 dependency admitted daily rows"
        )
    if {item.conflict_id for item in conflicts} != {
        "STAR_IPO_FIRST_FIVE_SESSIONS",
        "STAR_DELISTING_FIRST_SESSION",
    }:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "no-price-limit schema conflicts changed"
        )
    if any(item.code != "688086.SH" for item in conflicts):
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "no-price-limit sample scope changed"
        )


def _validate_rule_evidence(item: OfficialRuleEvidence) -> None:
    if not item.evidence_id or not item.exchange or not item.board:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "official rule evidence identity is incomplete"
        )
    if item.exchange not in {"SSE", "SZSE"}:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            "unsupported exchange in official rule evidence"
        )
    parsed = urlsplit(item.source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _OFFICIAL_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GP15PriceLimitSourceAssessmentBlockedError(
            f"rule evidence escaped official HTTPS origin: {item.evidence_id}"
        )
    published = (
        _iso_datetime(item.published_at, "published_at")
        if item.published_at is not None
        else None
    )
    effective = (
        _iso_date(item.effective_from, "effective_from")
        if item.effective_from is not None
        else None
    )
    if item.effective_to_exclusive is not None:
        effective_to = _iso_date(
            item.effective_to_exclusive, "effective_to_exclusive"
        )
        if effective is None or effective_to <= effective:
            raise GP15PriceLimitSourceAssessmentBlockedError(
                f"invalid effective interval: {item.evidence_id}"
            )
    if item.source_document_sha256 is not None:
        _sha256_text(item.source_document_sha256, "rule source document")
    if item.raw_document_cold_replayed and item.source_document_sha256 is None:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            f"cold-replayed evidence has no hash: {item.evidence_id}"
        )
    if item.point_in_time_fact_eligible:
        if (
            not item.raw_document_cold_replayed
            or item.source_document_sha256 is None
            or published is None
            or effective is None
            or published.date() > effective
        ):
            raise GP15PriceLimitSourceAssessmentBlockedError(
                f"point-in-time evidence is not fully bound: {item.evidence_id}"
            )
    if not item.observations or not item.limitations:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            f"rule evidence lacks observations or limitations: {item.evidence_id}"
        )


def _iso_date(value: str, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            f"{field_name} is not an ISO-8601 date"
        ) from exc
    if parsed.isoformat() != value:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            f"{field_name} is not canonical"
        )
    return parsed


def _iso_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            f"{field_name} is not an ISO-8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise GP15PriceLimitSourceAssessmentBlockedError(
            f"{field_name} is not a canonical timezone-aware datetime"
        )
    return parsed


def _sha256_text(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if _SHA256.fullmatch(digest) is None:
        raise GP15PriceLimitSourceAssessmentBlockedError(
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
