from __future__ import annotations

import re
from io import BytesIO
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from time import monotonic, sleep
from typing import Callable

from .models import LicenseClass, SourceRole
from .sources import SourceAdapter, SourceArtifact, SyncRequest
from .sources_official import (
    HTTPTransport,
    RequestsHTTPTransport,
    SourceFetchError,
    _parse_last_modified,
    _require_host,
    _require_payload,
    _require_sec_user_agent,
    _validate_observation_time,
)


SEC_FEE_URL = "https://www.sec.gov/rules-regulations/fee-rate-advisories"
FINRA_TAF_URL = (
    "https://www.finra.org/rules-guidance/rule-filings/"
    "sr-finra-2024-019/fee-adjustment-schedule"
)
FINRA_2012_NOTICE_URL = "https://www.finra.org/rules-guidance/notices/12-31"
FINRA_2020_FILING_URL = (
    "https://www.finra.org/sites/default/files/2020-10/"
    "NOF%20IMM%20EFF%20FINRA-2020-032.pdf"
)
FEE_EVIDENCE_CONTRACT_VERSION = 2


@dataclass(frozen=True)
class SECFeeEvidenceSpec:
    effective_from: date
    rate_per_dollar: float
    url: str
    published_at: datetime


@dataclass(frozen=True)
class FINRAFeeEvidenceSpec:
    url: str
    published_at: datetime
    entries: tuple[tuple[date, float, float], ...]


def _published(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


SEC_FEE_EVIDENCE = (
    SECFeeEvidenceSpec(
        date(2018, 5, 22),
        13.00 / 1_000_000,
        "https://www.sec.gov/newsroom/press-releases/2018-67",
        _published(2018, 4, 17),
    ),
    SECFeeEvidenceSpec(
        date(2019, 4, 16),
        20.70 / 1_000_000,
        "https://www.sec.gov/newsroom/press-releases/2019-30",
        _published(2019, 3, 12),
    ),
    SECFeeEvidenceSpec(
        date(2020, 2, 18),
        22.10 / 1_000_000,
        "https://www.sec.gov/newsroom/press-releases/2020-7",
        _published(2020, 1, 9),
    ),
    SECFeeEvidenceSpec(
        date(2021, 2, 25),
        5.10 / 1_000_000,
        "https://www.sec.gov/newsroom/press-releases/2021-8",
        _published(2021, 1, 15),
    ),
    SECFeeEvidenceSpec(
        date(2022, 5, 14),
        22.90 / 1_000_000,
        "https://www.sec.gov/newsroom/press-releases/2022-60",
        _published(2022, 4, 8),
    ),
    SECFeeEvidenceSpec(
        date(2023, 2, 27),
        8.00 / 1_000_000,
        "https://www.sec.gov/newsroom/press-releases/2023-15",
        _published(2023, 1, 23),
    ),
    SECFeeEvidenceSpec(
        date(2024, 5, 22),
        27.80 / 1_000_000,
        "https://www.sec.gov/rules-regulations/fee-rate-advisories/2024-2",
        _published(2024, 4, 17),
    ),
    SECFeeEvidenceSpec(
        date(2025, 5, 14),
        0.0,
        "https://www.sec.gov/rules-regulations/fee-rate-advisories/2025-2",
        _published(2025, 4, 8),
    ),
    SECFeeEvidenceSpec(
        date(2026, 4, 4),
        20.60 / 1_000_000,
        "https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2",
        _published(2026, 2, 27),
    ),
)

FINRA_FEE_EVIDENCE = (
    FINRAFeeEvidenceSpec(
        FINRA_2012_NOTICE_URL,
        _published(2012, 7, 2),
        ((date(2012, 7, 1), 0.000119, 5.95),),
    ),
    FINRAFeeEvidenceSpec(
        FINRA_2020_FILING_URL,
        _published(2020, 10, 20),
        (
            (date(2022, 1, 1), 0.000130, 6.49),
            (date(2023, 1, 1), 0.000145, 7.27),
            (date(2024, 1, 1), 0.000166, 8.30),
        ),
    ),
    FINRAFeeEvidenceSpec(
        FINRA_TAF_URL,
        _published(2024, 11, 21),
        (
            (date(2026, 1, 1), 0.000195, 9.79),
        ),
    ),
)


def _normalized_text(payload: bytes) -> str:
    if payload.lstrip().startswith(b"%PDF"):
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - dependency is deployment-checked
            raise SourceFetchError("pypdf is required to verify FINRA fee evidence") from exc
        try:
            text = " ".join(
                page.extract_text() or "" for page in PdfReader(BytesIO(payload)).pages
            )
        except Exception as exc:
            raise SourceFetchError("FINRA fee PDF could not be parsed deterministically") from exc
    else:
        text = payload.decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).casefold()


def _contains_number(text: str, value: float, *, decimals: int) -> bool:
    rendered = f"{value:.{decimals}f}"
    if value == 0:
        return bool(re.search(r"(?<![\d.])0(?:\.0+)?(?!\d)", text))
    variants = {rendered, rendered.rstrip("0").rstrip(".")}
    return any(
        item
        and re.search(rf"(?<![\d.]){re.escape(item)}(?!\d)", text)
        for item in variants
    )


def _validate_sec_payload(payload: bytes, spec: SECFeeEvidenceSpec) -> None:
    text = _normalized_text(payload)
    million_rate = spec.rate_per_dollar * 1_000_000
    date_tokens = {
        spec.effective_from.isoformat(),
        f"{spec.effective_from:%B} {spec.effective_from.day}, {spec.effective_from.year}".casefold(),
        f"{spec.effective_from:%b}. {spec.effective_from.day}, {spec.effective_from.year}".casefold(),
        f"{spec.effective_from:%b} {spec.effective_from.day}, {spec.effective_from.year}".casefold(),
    }
    if not any(token in text for token in date_tokens) or not _contains_number(
        text, million_rate, decimals=2
    ):
        raise SourceFetchError(
            "SEC fee advisory does not prove its declared effective date and rate"
        )


def _validate_finra_payload(payload: bytes, spec: FINRAFeeEvidenceSpec) -> None:
    text = _normalized_text(payload)
    for effective, per_share, cap in spec.entries:
        if str(effective.year) not in text:
            raise SourceFetchError("FINRA fee evidence is missing a declared effective year")
        if not _contains_number(text, per_share, decimals=6) or not _contains_number(
            text, cap, decimals=2
        ):
            raise SourceFetchError(
                "FINRA fee evidence does not prove its declared per-share rate and cap"
            )


def _active_specs(
    request: SyncRequest,
) -> tuple[tuple[SECFeeEvidenceSpec, ...], tuple[FINRAFeeEvidenceSpec, ...]]:
    active_sec = max(
        (
            candidate
            for candidate in SEC_FEE_EVIDENCE
            if candidate.effective_from <= request.start_date
        ),
        key=lambda candidate: candidate.effective_from,
        default=None,
    )
    sec = tuple(
        item
        for item in SEC_FEE_EVIDENCE
        if item.effective_from <= request.end_date
        and (item.effective_from >= request.start_date or item == active_sec)
    )
    needed_finra_dates = {
        item[0]
        for spec in FINRA_FEE_EVIDENCE
        for item in spec.entries
        if item[0] <= request.end_date
    }
    initial = max((item for item in needed_finra_dates if item <= request.start_date), default=None)
    needed_finra_dates = {
        item for item in needed_finra_dates if item >= request.start_date or item == initial
    }
    finra: list[FINRAFeeEvidenceSpec] = []
    for effective in sorted(needed_finra_dates):
        candidates = [
            spec
            for spec in FINRA_FEE_EVIDENCE
            if any(entry[0] == effective for entry in spec.entries)
        ]
        if not candidates:
            raise SourceFetchError(f"no official FINRA evidence for {effective.isoformat()}")
        preferred = candidates[-1] if effective >= date(2026, 1, 1) else candidates[0]
        if preferred not in finra:
            finra.append(preferred)
    return sec, tuple(finra)


class RegulatoryFeeEvidenceAdapter(SourceAdapter):
    """Freeze each official object that proves an effective historical fee row."""

    source_id = "us_regulatory_fee_official"
    source_version = "sec-finra-effective-v2"

    def __init__(
        self,
        *,
        sec_user_agent: str | None = None,
        transport: HTTPTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = 30.0,
        minimum_sec_request_interval_seconds: float = 0.12,
    ) -> None:
        self.sec_user_agent = _require_sec_user_agent(sec_user_agent)
        self.transport = transport or RequestsHTTPTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout_seconds = float(timeout_seconds)
        self.minimum_sec_request_interval_seconds = max(
            0.0, float(minimum_sec_request_interval_seconds)
        )

    def _fetch(
        self,
        url: str,
        *,
        authority: str,
        user_agent: str,
    ) -> tuple[bytes, str, datetime | None]:
        response = self.transport.get(
            url,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=self.timeout_seconds,
        )
        host = "www.sec.gov" if authority == "SEC" else "www.finra.org"
        _require_host(response.url, host, source=f"{authority} fee evidence")
        payload = _require_payload(response, source=f"{authority} fee evidence")
        media_type = response.headers.get("Content-Type", "application/octet-stream").split(
            ";", 1
        )[0]
        return payload, media_type, _parse_last_modified(
            response.headers, source=f"{authority} fee evidence"
        )

    def fetch(self, request: SyncRequest) -> tuple[SourceArtifact, ...]:
        _validate_observation_time(
            request.observed_at,
            clock=self.clock,
            tolerance=timedelta(minutes=5),
        )
        sec_specs, finra_specs = _active_specs(request)
        artifacts: list[SourceArtifact] = []
        last_sec_call: float | None = None
        for spec in sec_specs:
            if last_sec_call is not None:
                remaining = self.minimum_sec_request_interval_seconds - (
                    monotonic() - last_sec_call
                )
                if remaining > 0:
                    sleep(remaining)
            payload, media_type, _ = self._fetch(
                spec.url, authority="SEC", user_agent=self.sec_user_agent
            )
            last_sec_call = monotonic()
            _validate_sec_payload(payload, spec)
            artifacts.append(
                SourceArtifact(
                    dataset="regulatory_fee_sec",
                    payload=payload,
                    media_type=media_type,
                    url=spec.url,
                    observed_at=request.observed_at,
                    published_at=spec.published_at,
                    role=SourceRole.VALIDATION_ANCHOR,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "authority": "SEC",
                        "raw_frozen": True,
                        "verification_parser": (
                            "pypdf" if payload.lstrip().startswith(b"%PDF") else "utf8-text"
                        ),
                        "fee_evidence_contract_version": FEE_EVIDENCE_CONTRACT_VERSION,
                        "rate_entries": [
                            {
                                "effective_from": spec.effective_from.isoformat(),
                                "sec_sell_fee_rate": spec.rate_per_dollar,
                            }
                        ],
                        "sec_user_agent_configured": True,
                    },
                )
            )
        for spec in finra_specs:
            payload, media_type, _ = self._fetch(
                spec.url,
                authority="FINRA",
                user_agent="tdx-research-platform/0.1 fee-evidence",
            )
            _validate_finra_payload(payload, spec)
            artifacts.append(
                SourceArtifact(
                    dataset="regulatory_fee_finra",
                    payload=payload,
                    media_type=media_type,
                    url=spec.url,
                    observed_at=request.observed_at,
                    published_at=spec.published_at,
                    role=SourceRole.VALIDATION_ANCHOR,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "authority": "FINRA",
                        "raw_frozen": True,
                        "fee_evidence_contract_version": FEE_EVIDENCE_CONTRACT_VERSION,
                        "rate_entries": [
                            {
                                "effective_from": effective.isoformat(),
                                "finra_taf_per_share": per_share,
                                "finra_taf_cap": cap,
                            }
                            for effective, per_share, cap in spec.entries
                        ],
                    },
                )
            )
        return tuple(artifacts)


def fee_rate_entries(source_metadata: object) -> tuple[dict[str, object], ...]:
    if not isinstance(source_metadata, dict):
        return ()
    entries = source_metadata.get("rate_entries")
    if not isinstance(entries, list):
        return ()
    return tuple(dict(item) for item in entries if isinstance(item, dict))


__all__ = [
    "FEE_EVIDENCE_CONTRACT_VERSION",
    "FINRA_2012_NOTICE_URL",
    "FINRA_2020_FILING_URL",
    "FINRA_FEE_EVIDENCE",
    "FINRA_TAF_URL",
    "RegulatoryFeeEvidenceAdapter",
    "SEC_FEE_EVIDENCE",
    "SEC_FEE_URL",
    "fee_rate_entries",
]
