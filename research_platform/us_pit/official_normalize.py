from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import pandas as pd

from .hashing import canonical_json_bytes, sha256_bytes, sha256_file, sha256_json
from .models import LicenseClass, SourceDependency, SourceRole
from .sources_official import (
    SEC_NPORT_CIK,
    SEC_NPORT_SERIES_ID,
    SourceFetchError,
    SourcePolicyError,
    _validate_ivv_nport_filing,
)
from .store import USPITStore


NORMALIZATION_FORMAT_VERSION = "us-pit-official-normalization-v1"
_SEC_SOURCE_ID = "sec_nport_ivv"
_ISHARES_SOURCE_ID = "ishares_ivv_holdings"
_ISHARES_API_SOURCE_ID = "ishares_ivv_holdings_api"

HOLDING_COLUMNS = (
    "holding_candidate_id",
    "fund_ticker",
    "as_of_date",
    "published_at",
    "observed_at",
    "eligible_from",
    "signal_eligible",
    "source_id",
    "source_version",
    "content_sha256",
    "evidence_role",
    "license_class",
    "url",
    "source_row_number",
    "issuer_name",
    "lei",
    "cik",
    "title",
    "ticker",
    "exchange",
    "share_class",
    "cusip_raw",
    "cusip",
    "isin_raw",
    "isin",
    "identity_candidate_key",
    "asset_category",
    "currency",
    "quantity",
    "market_value_usd",
    "weight_percent",
)

IDENTITY_COLUMNS = (
    "identity_candidate_key",
    "holding_candidate_id",
    "issuer_name",
    "lei",
    "cik",
    "title",
    "ticker",
    "exchange",
    "share_class",
    "cusip_raw",
    "cusip",
    "isin_raw",
    "isin",
    "as_of_date",
    "observed_at",
    "source_id",
    "source_version",
    "content_sha256",
    "evidence_role",
    "url",
    "source_row_number",
)

ISSUE_COLUMNS = (
    "issue_id",
    "severity",
    "code",
    "source_id",
    "content_sha256",
    "source_row_number",
    "field",
    "value",
    "message",
    "requires_manual_review",
)


class OfficialNormalizationError(ValueError):
    """Official evidence cannot be normalized without weakening provenance."""


@dataclass(frozen=True)
class OfficialNormalizationResult:
    normalization_id: str
    path: Path
    manifest: Mapping[str, Any]

    @property
    def status(self) -> str:
        return str(self.manifest["status"])

    def load_frame(self, name: str) -> pd.DataFrame:
        descriptor = dict(self.manifest["artifacts"])[name]
        path = self.path / str(descriptor["filename"])
        if not path.is_file() or sha256_file(path) != descriptor["object_sha256"]:
            raise OfficialNormalizationError(f"normalization artifact is corrupt: {name}")
        return pd.read_parquet(path)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _element_value(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    for attribute in ("value", "val"):
        value = element.attrib.get(attribute)
        if value and value.strip():
            return value.strip()
    value = "".join(element.itertext()).strip()
    return value or None


def _descendant_value(element: ET.Element, names: Iterable[str]) -> str | None:
    wanted = {item.casefold() for item in names}
    for candidate in element.iter():
        if _local_name(candidate.tag) in wanted:
            value = _element_value(candidate)
            if value is not None:
                return value
    return None


def _xml_roots_from_complete_submission(payload: bytes) -> tuple[ET.Element, ...]:
    lowered_payload = payload.lower()
    if b"<!doctype" in lowered_payload or b"<!entity" in lowered_payload:
        raise OfficialNormalizationError("SEC XML declarations with entities are forbidden")

    blocks = re.findall(rb"<XML>\s*(.*?)\s*</XML>", payload, flags=re.I | re.S)
    if not blocks:
        blocks = re.findall(rb"<TEXT>\s*(.*?)\s*</TEXT>", payload, flags=re.I | re.S)
    roots: list[ET.Element] = []
    parse_errors: list[str] = []
    for block in blocks:
        if b"invstorsec" not in block.lower():
            continue
        try:
            root = ET.fromstring(block.strip())
        except ET.ParseError as exc:
            parse_errors.append(str(exc))
            continue
        series_ids = {
            value.upper()
            for item in root.iter()
            if _local_name(item.tag) == "seriesid"
            for value in [_element_value(item)]
            if value
        }
        if series_ids and series_ids != {SEC_NPORT_SERIES_ID}:
            raise OfficialNormalizationError(
                "SEC N-PORT XML does not identify exactly the IVV series"
            )
        roots.append(root)
    if not roots:
        detail = f": {parse_errors[0]}" if parse_errors else ""
        raise OfficialNormalizationError(
            f"SEC complete submission contains no parseable N-PORT holdings XML{detail}"
        )
    return tuple(roots)


def _normalized_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[\s-]+", "", value).upper()
    if normalized in {"", "N/A", "NA", "NONE", "NULL", "-", "000000000"}:
        return None
    return normalized


def _normalized_cik(value: str | None) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    return digits.zfill(10) if digits else None


def _cusip_valid(value: str | None) -> bool:
    if value is None or re.fullmatch(r"[0-9A-Z*@#]{9}", value) is None:
        return False
    values: list[int] = []
    for character in value[:8]:
        if character.isdigit():
            values.append(int(character))
        elif character.isalpha():
            values.append(ord(character) - ord("A") + 10)
        else:
            values.append({"*": 36, "@": 37, "#": 38}[character])
    total = 0
    for index, number in enumerate(values):
        product = number * (2 if index % 2 else 1)
        total += product // 10 + product % 10
    return str((10 - total % 10) % 10) == value[-1]


def _isin_valid(value: str | None) -> bool:
    if value is None or re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value) is None:
        return False
    expanded = "".join(
        character if character.isdigit() else str(ord(character) - ord("A") + 10)
        for character in value
    )
    total = 0
    for index, character in enumerate(reversed(expanded)):
        number = int(character) * (2 if index % 2 else 1)
        total += number // 10 + number % 10
    return total % 10 == 0


def _share_class(*values: str | None) -> str | None:
    text = " ".join(value or "" for value in values).upper()
    # A trailing product label such as "... NV CLASS LYONDELLBASELL" must not
    # turn the issuer name into a share class. Exchange share-class labels are
    # compact (A, B, C, I, A1, etc.); longer words require manual evidence.
    match = re.search(r"\b(?:CLASS|CL)\s+([A-Z0-9]{1,3})\b", text)
    if match is None:
        match = re.search(r"\b([A-Z])\s+SHARES?\b", text)
    return None if match is None else match.group(1)


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.strip().replace(",", "").replace("$", "").replace("%", "")
    if not cleaned or cleaned in {"-", "--"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _issue(
    dependency: SourceDependency,
    row_number: int,
    *,
    severity: str,
    code: str,
    field: str,
    value: str | None,
    message: str,
    review: bool,
) -> dict[str, Any]:
    identity = {
        "source_sha256": dependency.object_sha256,
        "row_number": row_number,
        "code": code,
        "field": field,
        "value": value,
    }
    return {
        "issue_id": sha256_json(identity),
        "severity": severity,
        "code": code,
        "source_id": dependency.source_id,
        "content_sha256": dependency.object_sha256,
        "source_row_number": row_number,
        "field": field,
        "value": value,
        "message": message,
        "requires_manual_review": review,
    }


def _identity_values(
    dependency: SourceDependency,
    row_number: int,
    *,
    cusip_raw: str | None,
    isin_raw: str | None,
    issues: list[dict[str, Any]],
) -> tuple[str | None, str | None, str | None]:
    cusip_value = _normalized_identifier(cusip_raw)
    isin_value = _normalized_identifier(isin_raw)
    cusip = cusip_value if _cusip_valid(cusip_value) else None
    isin = isin_value if _isin_valid(isin_value) else None
    if cusip_value is not None and cusip is None:
        issues.append(
            _issue(
                dependency,
                row_number,
                severity="HIGH",
                code="INVALID_CUSIP",
                field="cusip",
                value=cusip_raw,
                message="CUSIP failed syntax or check-digit validation.",
                review=True,
            )
        )
    if isin_value is not None and isin is None:
        issues.append(
            _issue(
                dependency,
                row_number,
                severity="HIGH",
                code="INVALID_ISIN",
                field="isin",
                value=isin_raw,
                message="ISIN failed syntax or check-digit validation.",
                review=True,
            )
        )
    if cusip is None and isin is None:
        issues.append(
            _issue(
                dependency,
                row_number,
                severity="HIGH",
                code="MISSING_STABLE_IDENTIFIER",
                field="cusip,isin",
                value=None,
                message="Neither a valid ISIN nor a valid CUSIP identifies this holding.",
                review=True,
            )
        )
    key = f"isin:{isin}" if isin is not None else (f"cusip:{cusip}" if cusip else None)
    return cusip, isin, key


def _base_holding(
    dependency: SourceDependency,
    row_number: int,
    *,
    as_of_date: date,
    issuer_name: str | None,
    lei: str | None,
    cik: str | None,
    title: str | None,
    ticker: str | None,
    exchange: str | None,
    cusip_raw: str | None,
    isin_raw: str | None,
    asset_category: str | None,
    currency: str | None,
    quantity: float | None,
    market_value_usd: float | None,
    weight_percent: float | None,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    cusip, isin, identity_key = _identity_values(
        dependency,
        row_number,
        cusip_raw=cusip_raw,
        isin_raw=isin_raw,
        issues=issues,
    )
    signal_eligible = dependency.role == SourceRole.SIGNAL_INPUT
    eligible_from = dependency.observed_at if signal_eligible else None
    candidate_identity = {
        "source_sha256": dependency.object_sha256,
        "source_row_number": row_number,
        "as_of_date": as_of_date.isoformat(),
    }
    return {
        "holding_candidate_id": sha256_json(candidate_identity),
        "fund_ticker": "IVV",
        "as_of_date": as_of_date.isoformat(),
        "published_at": dependency.published_at,
        "observed_at": dependency.observed_at,
        "eligible_from": eligible_from,
        "signal_eligible": signal_eligible,
        "source_id": dependency.source_id,
        "source_version": dependency.source_version,
        "content_sha256": dependency.object_sha256,
        "evidence_role": dependency.role.value,
        "license_class": dependency.license_class.value,
        "url": dependency.url,
        "source_row_number": row_number,
        "issuer_name": issuer_name,
        "lei": _normalized_identifier(lei),
        "cik": _normalized_cik(cik),
        "title": title,
        "ticker": ticker,
        "exchange": exchange,
        "share_class": _share_class(title, issuer_name),
        "cusip_raw": cusip_raw,
        "cusip": cusip,
        "isin_raw": isin_raw,
        "isin": isin,
        "identity_candidate_key": identity_key,
        "asset_category": asset_category,
        "currency": currency,
        "quantity": quantity,
        "market_value_usd": market_value_usd,
        "weight_percent": weight_percent,
    }


def _parse_sec(
    payload: bytes,
    dependency: SourceDependency,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metadata = dict(dependency.metadata)
    accession = str(metadata.get("accession_number") or "")
    observed_at = datetime.fromisoformat(dependency.observed_at.replace("Z", "+00:00"))
    try:
        filing = _validate_ivv_nport_filing(
            payload,
            expected_accession=accession,
            observed_at=observed_at,
        )
    except SourceFetchError as exc:
        raise OfficialNormalizationError(str(exc)) from exc
    if filing["report_date"].isoformat() != dependency.as_of_date:
        raise OfficialNormalizationError("SEC report date disagrees with source lineage")
    if filing["accepted_at"].isoformat() != dependency.published_at:
        raise OfficialNormalizationError("SEC acceptance time disagrees with source lineage")

    holdings: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    row_number = 0
    for root in _xml_roots_from_complete_submission(payload):
        for investment in root.iter():
            if _local_name(investment.tag) != "invstorsec":
                continue
            row_number += 1
            asset_category = _descendant_value(investment, ("assetCat", "assetCategory"))
            if (asset_category or "").strip().upper() not in {
                "EC",
                "COMMON EQUITY",
                "EQUITY-COMMON",
                "COMMON STOCK",
            }:
                issues.append(
                    _issue(
                        dependency,
                        row_number,
                        severity="LOW",
                        code="NON_COMMON_EQUITY_FILTERED",
                        field="asset_category",
                        value=asset_category,
                        message="Holding was excluded because SEC did not classify it as common equity.",
                        review=False,
                    )
                )
                continue
            holdings.append(
                _base_holding(
                    dependency,
                    row_number,
                    as_of_date=filing["report_date"],
                    issuer_name=_descendant_value(investment, ("name", "issuerName")),
                    lei=_descendant_value(investment, ("lei", "issuerLei")),
                    cik=_descendant_value(investment, ("cik", "issuerCik")),
                    title=_descendant_value(investment, ("title",)),
                    ticker=_descendant_value(investment, ("ticker",)),
                    exchange=None,
                    cusip_raw=_descendant_value(investment, ("cusip",)),
                    isin_raw=_descendant_value(investment, ("isin",)),
                    asset_category=asset_category,
                    currency=_descendant_value(
                        investment, ("curCd", "currencyCode", "currency")
                    ),
                    quantity=_number(_descendant_value(investment, ("balance",))),
                    market_value_usd=_number(
                        _descendant_value(investment, ("valUSD", "valueUSD"))
                    ),
                    weight_percent=_number(
                        _descendant_value(investment, ("pctVal", "percentage"))
                    ),
                    issues=issues,
                )
            )
    if row_number == 0:
        raise OfficialNormalizationError("SEC N-PORT XML contains no investment rows")
    return holdings, issues


def _column_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold().replace("%", "percent"))


def _csv_value(row: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = row.get(_column_key(name))
        if value is not None and value.strip():
            return value.strip()
    return None


def _parse_date(value: str) -> date | None:
    cleaned = value.strip().strip('"')
    for pattern in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
    return None


def _parse_ishares(
    payload: bytes,
    dependency: SourceDependency,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        records = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise OfficialNormalizationError("iShares holdings CSV is not parseable") from exc
    header_index: int | None = None
    payload_as_of: date | None = None
    for index, record in enumerate(records):
        keys = {_column_key(value) for value in record}
        if {"ticker", "name"}.issubset(keys) and "assetclass" in keys:
            header_index = index
            break
        if record and "fundholdingsasof" in _column_key(record[0]):
            candidates = record[1:] or [record[0].split("as of", 1)[-1]]
            for candidate in candidates:
                payload_as_of = _parse_date(candidate)
                if payload_as_of is not None:
                    break
    if header_index is None:
        raise OfficialNormalizationError("iShares CSV has no holdings header")
    lineage_as_of = None if dependency.as_of_date is None else date.fromisoformat(
        dependency.as_of_date
    )
    if lineage_as_of is not None and payload_as_of is not None and lineage_as_of != payload_as_of:
        raise OfficialNormalizationError("iShares as-of date disagrees with source lineage")
    as_of_date = lineage_as_of or payload_as_of
    if as_of_date is None:
        raise OfficialNormalizationError("iShares current snapshot has no parseable as-of date")

    header = [_column_key(value) for value in records[header_index]]
    holdings: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for source_row_number, values in enumerate(records[header_index + 1 :], header_index + 2):
        if not any(value.strip() for value in values):
            continue
        padded = values + [""] * max(0, len(header) - len(values))
        row = dict(zip(header, padded, strict=False))
        asset_class = _csv_value(row, "Asset Class")
        ticker = _csv_value(row, "Ticker")
        if (asset_class or "").strip().casefold() != "equity":
            issues.append(
                _issue(
                    dependency,
                    source_row_number,
                    severity="LOW",
                    code="NON_COMMON_EQUITY_FILTERED",
                    field="asset_class",
                    value=asset_class,
                    message="Holding was excluded because iShares did not classify it as equity.",
                    review=False,
                )
            )
            continue
        holdings.append(
            _base_holding(
                dependency,
                source_row_number,
                as_of_date=as_of_date,
                issuer_name=_csv_value(row, "Name", "Issuer Name"),
                lei=_csv_value(row, "LEI"),
                cik=_csv_value(row, "CIK"),
                title=_csv_value(row, "Title", "Security Description"),
                ticker=ticker,
                exchange=_csv_value(row, "Exchange"),
                cusip_raw=_csv_value(row, "CUSIP"),
                isin_raw=_csv_value(row, "ISIN"),
                asset_category=asset_class,
                currency=_csv_value(row, "Currency", "Market Currency"),
                quantity=_number(_csv_value(row, "Quantity", "Shares")),
                market_value_usd=_number(_csv_value(row, "Market Value")),
                weight_percent=_number(_csv_value(row, "Weight (%)", "Weight")),
                issues=issues,
            )
        )
    if not holdings:
        raise OfficialNormalizationError("iShares CSV contains no common-equity candidates")
    return holdings, issues


def _product_data_array(points: Mapping[str, Any], name: str) -> list[Any]:
    point = points.get(name)
    if not isinstance(point, Mapping):
        raise OfficialNormalizationError(f"iShares product data is missing {name}")
    values = point.get("value")
    if not isinstance(values, list):
        raise OfficialNormalizationError(f"iShares product data {name} is not an array")
    return values


def _product_text(values: list[Any], index: int) -> str | None:
    value = values[index]
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _product_number(values: list[Any], index: int) -> float | None:
    value = values[index]
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ishares_product_data(
    payload: bytes,
    dependency: SourceDependency,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        document = json.loads(payload)
        points = document["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]["dataPointsByNameMap"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise OfficialNormalizationError("iShares product data is not parseable") from exc
    if str(document.get("productId")) != "239726" or not isinstance(points, Mapping):
        raise OfficialNormalizationError("iShares product data has the wrong product or shape")
    as_of = points.get("asOfDate")
    if not isinstance(as_of, Mapping):
        raise OfficialNormalizationError("iShares product data has no as-of date")
    payload_as_of = _parse_date(str(as_of.get("formattedValue") or ""))
    if payload_as_of is None:
        raw_as_of = str(as_of.get("value") or "")
        try:
            payload_as_of = datetime.strptime(raw_as_of, "%Y%m%d").date()
        except ValueError as exc:
            raise OfficialNormalizationError("iShares product data as-of date is invalid") from exc
    if dependency.as_of_date != payload_as_of.isoformat():
        raise OfficialNormalizationError("iShares product-data date disagrees with source lineage")

    names = (
        "ticker", "issueName", "assetClass", "cusip", "isin", "exchange",
        "currencyCode", "unitsHeld", "marketValue", "holdingPercent",
    )
    arrays = {name: _product_data_array(points, name) for name in names}
    lengths = {len(values) for values in arrays.values()}
    if len(lengths) != 1 or next(iter(lengths), 0) < 400:
        raise OfficialNormalizationError("iShares product-data arrays are incomplete")

    holdings: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for index in range(next(iter(lengths))):
        row_number = index + 1
        asset_class = _product_text(arrays["assetClass"], index)
        if (asset_class or "").casefold() != "equity":
            issues.append(
                _issue(
                    dependency,
                    row_number,
                    severity="LOW",
                    code="NON_COMMON_EQUITY_FILTERED",
                    field="asset_class",
                    value=asset_class,
                    message="Holding was excluded because iShares did not classify it as equity.",
                    review=False,
                )
            )
            continue
        issuer_name = _product_text(arrays["issueName"], index)
        holdings.append(
            _base_holding(
                dependency,
                row_number,
                as_of_date=payload_as_of,
                issuer_name=issuer_name,
                lei=None,
                cik=None,
                title=issuer_name,
                ticker=_product_text(arrays["ticker"], index),
                exchange=_product_text(arrays["exchange"], index),
                cusip_raw=_product_text(arrays["cusip"], index),
                isin_raw=_product_text(arrays["isin"], index),
                asset_category=asset_class,
                currency=_product_text(arrays["currencyCode"], index),
                quantity=_product_number(arrays["unitsHeld"], index),
                market_value_usd=_product_number(arrays["marketValue"], index),
                weight_percent=_product_number(arrays["holdingPercent"], index),
                issues=issues,
            )
        )
    if not holdings:
        raise OfficialNormalizationError("iShares product data has no common-equity candidates")
    return holdings, issues


def _frame(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(columns))
    for column in columns:
        if column in {"signal_eligible", "requires_manual_review"}:
            frame[column] = frame[column].astype("boolean")
        elif column in {"source_row_number"}:
            frame[column] = frame[column].astype("Int64")
        elif column in {"quantity", "market_value_usd", "weight_percent"}:
            frame[column] = frame[column].astype("Float64")
        else:
            frame[column] = frame[column].astype("string")
    return frame


def _identity_frame(holdings: pd.DataFrame) -> pd.DataFrame:
    frame = holdings.loc[:, [item for item in IDENTITY_COLUMNS]].copy()
    return frame.sort_values(
        ["content_sha256", "source_row_number"], kind="stable"
    ).reset_index(drop=True)


class OfficialHoldingsNormalizationService:
    """Publish review-only candidates derived from hash-verified official raw objects."""

    def __init__(self, store: USPITStore | Path | str) -> None:
        self.store = store if isinstance(store, USPITStore) else USPITStore(store)
        self.root = self.store.root / "normalized" / "official"
        self.staging = self.root / ".staging"
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    def normalize(self, source_batch_ids: Iterable[str]) -> OfficialNormalizationResult:
        batch_ids = tuple(sorted(set(str(item).strip() for item in source_batch_ids)))
        if not batch_ids or any(not item for item in batch_ids):
            raise OfficialNormalizationError("at least one source batch id is required")
        dependencies: dict[tuple[str, str], SourceDependency] = {}
        for batch_id in batch_ids:
            batch = self.store.load_source_batch(batch_id)
            for dependency in batch.dependencies:
                key = (dependency.source_id, dependency.object_sha256)
                existing = dependencies.get(key)
                if existing is not None and existing.to_dict() != dependency.to_dict():
                    raise OfficialNormalizationError(
                        "the same official object has conflicting source lineage"
                    )
                dependencies[key] = dependency

        selected: list[SourceDependency] = []
        holding_rows: list[dict[str, Any]] = []
        issue_rows: list[dict[str, Any]] = []
        dependency_values = self._supersede_sec_amendments(
            tuple(dependencies.values())
        )
        for dependency in sorted(
            dependency_values,
            key=lambda item: (item.source_id, item.as_of_date or "", item.object_sha256),
        ):
            metadata = dict(dependency.metadata)
            artifact_kind = str(metadata.get("artifact_kind") or "")
            if dependency.source_id == _SEC_SOURCE_ID and artifact_kind == "raw_complete_edgar_submission":
                self._validate_sec_dependency(dependency)
                parser = _parse_sec
            elif dependency.source_id == _ISHARES_SOURCE_ID and artifact_kind == "raw_observed_holdings_csv":
                self._validate_ishares_dependency(dependency)
                parser = _parse_ishares
            elif (
                dependency.source_id == _ISHARES_API_SOURCE_ID
                and artifact_kind == "raw_historical_holdings_product_data_json"
            ):
                self._validate_ishares_dependency(dependency)
                parser = _parse_ishares_product_data
            else:
                continue
            object_path = self.store.object_path(dependency.object_sha256)
            if not object_path.is_file() or sha256_file(object_path) != dependency.object_sha256:
                raise OfficialNormalizationError(
                    f"captured source object is missing or corrupt: {dependency.object_sha256}"
                )
            rows, issues = parser(object_path.read_bytes(), dependency)
            selected.append(dependency)
            holding_rows.extend(rows)
            issue_rows.extend(issues)

        if not selected:
            raise OfficialNormalizationError(
                "source batches contain no SEC IVV filing or iShares holdings raw object"
            )
        self._append_cross_row_issues(holding_rows, issue_rows, selected)
        holdings = _frame(holding_rows, HOLDING_COLUMNS).sort_values(
            ["as_of_date", "source_id", "content_sha256", "source_row_number"],
            kind="stable",
        ).reset_index(drop=True)
        identities = _identity_frame(holdings)
        issues = _frame(issue_rows, ISSUE_COLUMNS).sort_values(
            ["severity", "code", "content_sha256", "source_row_number"],
            kind="stable",
        ).reset_index(drop=True)
        return self._publish(batch_ids, tuple(selected), holdings, identities, issues)

    @staticmethod
    def _supersede_sec_amendments(
        dependencies: tuple[SourceDependency, ...],
    ) -> tuple[SourceDependency, ...]:
        passthrough: list[SourceDependency] = []
        filings: dict[str, list[SourceDependency]] = {}
        for dependency in dependencies:
            metadata = dict(dependency.metadata)
            if (
                dependency.source_id == _SEC_SOURCE_ID
                and metadata.get("artifact_kind")
                == "raw_complete_edgar_submission"
                and dependency.as_of_date is not None
            ):
                filings.setdefault(dependency.as_of_date, []).append(dependency)
            else:
                passthrough.append(dependency)
        for report_date, values in filings.items():
            ordered = sorted(
                values,
                key=lambda item: (
                    pd.Timestamp(item.published_at),
                    str(dict(item.metadata).get("accession_number") or ""),
                ),
            )
            latest_time = pd.Timestamp(ordered[-1].published_at)
            latest = [
                item
                for item in ordered
                if pd.Timestamp(item.published_at) == latest_time
            ]
            if len(latest) != 1:
                raise OfficialNormalizationError(
                    "SEC filings for one report date have an ambiguous latest amendment: "
                    + report_date
                )
            winner = latest[0]
            form = str(dict(winner.metadata).get("form") or "")
            if len(values) > 1 and form != "NPORT-P/A":
                raise OfficialNormalizationError(
                    "later SEC filing does not identify itself as NPORT-P/A: "
                    + report_date
                )
            passthrough.append(winner)
        return tuple(passthrough)

    @staticmethod
    def _validate_sec_dependency(dependency: SourceDependency) -> None:
        metadata = dict(dependency.metadata)
        OfficialHoldingsNormalizationService._validate_common_dependency(dependency)
        if dependency.license_class != LicenseClass.OFFICIAL_PUBLIC:
            raise SourcePolicyError("SEC normalization requires OFFICIAL_PUBLIC evidence")
        if dependency.role != SourceRole.VALIDATION_ANCHOR:
            raise SourcePolicyError("SEC N-PORT may only be a validation anchor")
        if metadata.get("registrant_cik") != SEC_NPORT_CIK:
            raise SourcePolicyError("SEC source lineage has the wrong registrant CIK")
        if metadata.get("series_id") != SEC_NPORT_SERIES_ID:
            raise SourcePolicyError("SEC source lineage has the wrong IVV series")
        if metadata.get("eligible_for_historical_signal") is not False:
            raise SourcePolicyError("SEC late filings cannot be historical signal input")
        if dependency.as_of_date is None or dependency.published_at is None:
            raise SourcePolicyError("SEC filing lineage lacks report or acceptance time")

    @staticmethod
    def _validate_ishares_dependency(dependency: SourceDependency) -> None:
        metadata = dict(dependency.metadata)
        OfficialHoldingsNormalizationService._validate_common_dependency(dependency)
        if dependency.license_class != LicenseClass.OFFICIAL_PUBLIC:
            raise SourcePolicyError("iShares normalization requires OFFICIAL_PUBLIC evidence")
        mode = str(metadata.get("observation_mode") or "")
        if mode == "current":
            if dependency.role != SourceRole.SIGNAL_INPUT:
                raise SourcePolicyError("current iShares observation must be SIGNAL_INPUT")
            if metadata.get("eligible_from") != dependency.observed_at:
                raise SourcePolicyError("iShares signal eligibility must begin at observed_at")
            if dependency.published_at != dependency.observed_at:
                raise SourcePolicyError(
                    "iShares current publication boundary must equal first observed_at"
                )
            if metadata.get("eligible_for_historical_signal") is not True:
                raise SourcePolicyError(
                    "iShares current observation must be eligible from observed_at"
                )
            if dependency.as_of_date is None:
                raise SourcePolicyError(
                    "iShares current observation requires the payload as-of date"
                )
        elif mode == "historical_as_of_reconciliation":
            if dependency.role != SourceRole.VALIDATION_ANCHOR:
                raise SourcePolicyError("historical iShares response is validation-only")
            if metadata.get("historical_publication_time_proven") is not False:
                raise SourcePolicyError("historical iShares publication must remain unproven")
            if metadata.get("eligible_for_historical_signal") is not False:
                raise SourcePolicyError("historical iShares response cannot become signal input")
            if metadata.get("eligible_from") is not None:
                raise SourcePolicyError("historical iShares response cannot claim an eligible_from")
            if dependency.source_id == _ISHARES_API_SOURCE_ID:
                if dependency.published_at != dependency.observed_at:
                    raise SourcePolicyError(
                        "late-observed iShares product data must use observed_at as its availability boundary"
                    )
                if metadata.get("product_id") not in {None, "239726"}:
                    raise SourcePolicyError("iShares product-data lineage has the wrong product id")
        else:
            raise SourcePolicyError("unknown iShares observation mode")

    @staticmethod
    def _validate_common_dependency(dependency: SourceDependency) -> None:
        if dependency.dataset != "fund_holdings_observed":
            raise SourcePolicyError("official holdings object has the wrong dataset")
        response_sha256 = dict(dependency.metadata).get("response_sha256")
        if response_sha256 != dependency.object_sha256:
            raise SourcePolicyError(
                "official response SHA-256 does not match its captured CAS object"
            )

    @staticmethod
    def _append_cross_row_issues(
        holdings: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        dependencies: list[SourceDependency],
    ) -> None:
        dependency_by_hash = {item.object_sha256: item for item in dependencies}
        by_snapshot: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in holdings:
            key = (
                str(row["content_sha256"]),
                str(row["as_of_date"]),
                str(row["identity_candidate_key"] or ""),
            )
            if key[2]:
                by_snapshot.setdefault(key, []).append(row)
        for (digest, _as_of, identity_key), values in by_snapshot.items():
            if len(values) <= 1:
                continue
            dependency = dependency_by_hash[digest]
            for row in values:
                issues.append(
                    _issue(
                        dependency,
                        int(row["source_row_number"]),
                        severity="HIGH",
                        code="DUPLICATE_IDENTITY_IN_SNAPSHOT",
                        field="identity_candidate_key",
                        value=identity_key,
                        message="A stable identifier occurs more than once in one holdings snapshot.",
                        review=True,
                    )
                )

        for row in holdings:
            cusip = row.get("cusip")
            isin = row.get("isin")
            if cusip and isin and str(isin).startswith("US") and str(isin)[2:11] != cusip:
                dependency = dependency_by_hash[str(row["content_sha256"])]
                issues.append(
                    _issue(
                        dependency,
                        int(row["source_row_number"]),
                        severity="HIGH",
                        code="CUSIP_ISIN_CONFLICT",
                        field="cusip,isin",
                        value=f"{cusip}|{isin}",
                        message="US ISIN does not embed the reported CUSIP.",
                        review=True,
                    )
                )

        by_cusip: dict[str, list[dict[str, Any]]] = {}
        by_identity: dict[str, list[dict[str, Any]]] = {}
        for row in holdings:
            if row.get("cusip"):
                by_cusip.setdefault(str(row["cusip"]), []).append(row)
            if row.get("identity_candidate_key"):
                by_identity.setdefault(str(row["identity_candidate_key"]), []).append(row)
        for cusip, values in by_cusip.items():
            isins = {str(item["isin"]) for item in values if item.get("isin")}
            if len(isins) <= 1:
                continue
            for row in values:
                dependency = dependency_by_hash[str(row["content_sha256"])]
                issues.append(
                    _issue(
                        dependency,
                        int(row["source_row_number"]),
                        severity="HIGH",
                        code="CUSIP_TO_MULTIPLE_ISIN",
                        field="cusip",
                        value=cusip,
                        message="One CUSIP maps to multiple valid ISIN values.",
                        review=True,
                    )
                )
        for identity_key, values in by_identity.items():
            classes = {str(item["share_class"]) for item in values if item.get("share_class")}
            if len(classes) <= 1:
                continue
            for row in values:
                dependency = dependency_by_hash[str(row["content_sha256"])]
                issues.append(
                    _issue(
                        dependency,
                        int(row["source_row_number"]),
                        severity="HIGH",
                        code="IDENTIFIER_SHARE_CLASS_CONFLICT",
                        field="share_class",
                        value=str(row.get("share_class") or ""),
                        message="One stable identifier is associated with multiple share classes.",
                        review=True,
                    )
                )

    def _publish(
        self,
        batch_ids: tuple[str, ...],
        dependencies: tuple[SourceDependency, ...],
        holdings: pd.DataFrame,
        identities: pd.DataFrame,
        issues: pd.DataFrame,
    ) -> OfficialNormalizationResult:
        frames = {
            "fund_holdings_observed_candidate": holdings,
            "security_identity_candidates": identities,
            "normalization_issues": issues,
        }
        references = {name: self.store.put_dataframe(frame) for name, frame in frames.items()}
        descriptors = {
            name: {
                "filename": f"{name}.parquet",
                "object_sha256": reference.sha256,
                "size_bytes": reference.size_bytes,
                "row_count": reference.row_count,
                "schema_sha256": reference.schema_sha256,
                "media_type": reference.media_type,
            }
            for name, reference in references.items()
        }
        observed_times = [
            datetime.fromisoformat(item.observed_at.replace("Z", "+00:00"))
            for item in dependencies
        ]
        created_at = max(observed_times).astimezone(timezone.utc).isoformat()
        severity_counts = {
            severity: int((issues["severity"] == severity).sum())
            for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        }
        identity_payload = {
            "format_version": NORMALIZATION_FORMAT_VERSION,
            "source_batch_ids": list(batch_ids),
            "sources": [item.to_dict() for item in dependencies],
            "artifacts": descriptors,
            "policy": {
                "sec_role": SourceRole.VALIDATION_ANCHOR.value,
                "sec_historical_signal_eligible": False,
                "ishares_current_eligible_from": "observed_at",
                "stable_identity_priority": ["ISIN", "CUSIP"],
                "ticker_or_name_identity_forbidden": True,
                "common_equity_only": True,
            },
        }
        normalization_id = sha256_json(identity_payload)
        manifest = {
            **identity_payload,
            "normalization_id": normalization_id,
            "created_at": created_at,
            "status": "REVIEW_REQUIRED",
            "release_status": "DATA_BLOCKED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "counts": {
                "holding_candidates": len(holdings),
                "identity_candidates": len(identities),
                "issues": len(issues),
                "issues_by_severity": severity_counts,
                "unresolved_identity_candidates": int(
                    holdings["identity_candidate_key"].isna().sum()
                ),
            },
            "review_requirements": [
                "resolve every HIGH or CRITICAL normalization issue",
                "map candidates to reviewed stable security_id values",
                "reconcile quarterly SEC anchors without treating them as historical signals",
                "derive membership events and monthly membership in a separate reviewed workspace",
            ],
        }
        manifest_bytes = canonical_json_bytes(manifest)
        target = self.root / normalization_id
        if target.exists():
            return self._verify_existing(target, manifest_bytes, manifest)

        stage = self.staging / f"{normalization_id}.{uuid4().hex}"
        stage.mkdir(parents=False, exist_ok=False)
        try:
            for name, reference in references.items():
                destination = stage / descriptors[name]["filename"]
                shutil.copy2(reference.path, destination)
                destination.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            manifest_path = stage / "manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            manifest_path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            try:
                os.rename(stage, target)
            except FileExistsError:
                return self._verify_existing(target, manifest_bytes, manifest)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return OfficialNormalizationResult(normalization_id, target, manifest)

    @staticmethod
    def _verify_existing(
        target: Path,
        expected_manifest_bytes: bytes,
        manifest: Mapping[str, Any],
    ) -> OfficialNormalizationResult:
        manifest_path = target / "manifest.json"
        if not manifest_path.is_file() or manifest_path.read_bytes() != expected_manifest_bytes:
            raise OfficialNormalizationError(
                f"normalization package identity conflict: {target.name}"
            )
        for name, descriptor_value in dict(manifest["artifacts"]).items():
            descriptor = dict(descriptor_value)
            path = target / str(descriptor["filename"])
            if not path.is_file() or sha256_file(path) != descriptor["object_sha256"]:
                raise OfficialNormalizationError(
                    f"normalization artifact is missing or corrupt: {name}"
                )
        return OfficialNormalizationResult(target.name, target, manifest)


__all__ = [
    "HOLDING_COLUMNS",
    "IDENTITY_COLUMNS",
    "ISSUE_COLUMNS",
    "NORMALIZATION_FORMAT_VERSION",
    "OfficialHoldingsNormalizationService",
    "OfficialNormalizationError",
    "OfficialNormalizationResult",
]
