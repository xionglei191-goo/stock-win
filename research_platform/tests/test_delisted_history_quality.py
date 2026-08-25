from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research_platform import csrc_industry_history_source as csrc_industry
from research_platform import cninfo_delisted_disclosures as cninfo
from research_platform import official_trading_calendar as official_calendar
from research_platform.cninfo_announcement_quality_adapter import (
    build_cninfo_announcement_documents_quality_index,
)
from research_platform.delisted_history_quality import (
    AUDIT_END,
    AUDIT_START,
    DATASET_CONTRACTS,
    DELISTED_HISTORY_QUALITY_REJECTED,
    DELISTED_HISTORY_SOURCE_INCOMPLETE,
    DelistedHistoryQualityCAS,
    DelistedHistoryQualityBlockedError,
    READY,
    RAW_AUTHORITY_BY_DATASET_EXCHANGE,
    RAW_ENVELOPE_PROTOCOL_VERSION,
    ROW_SOURCE_HASH_FIELDS,
    SOURCE_INDEX_AUTHORITY,
    SOURCE_INDEX_PROTOCOL_VERSION,
    audit_delisted_history,
    load_verified_delisted_history_gate,
)
from research_platform.tests.test_cninfo_delisted_disclosures import (
    _Session as _CninfoSession,
    _announcement_row,
    _pdf,
)
from research_platform.tests.test_official_trading_calendar import (
    FIXED_NOW as CALENDAR_FIXED_NOW,
    _fixture_session as _calendar_fixture_session,
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    return b"\n".join(_canonical(row) for row in rows) + b"\n"


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _minimal_pdf(lines: list[str]) -> bytes:
    operators = ["BT /F1 12 Tf 72 720 Td"]
    for position, line in enumerate(lines):
        if position:
            operators.append("0 -20 Td")
        operators.append(f"({line}) Tj")
    operators.append("ET")
    stream = " ".join(operators).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode("ascii"))
        content.extend(value)
        content.extend(b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(content)


_SYNTHETIC_INDUSTRY_PDF = _minimal_pdf(
    [
        "Manufacturing (I) 01 600001 Synthetic",
        "Mineral (C) 30 000511 Synthetic",
    ]
)


def _cninfo_stock_master_bytes(code: str, org_id: str) -> bytes:
    rows = [
        {
            "category": "A_SHARE",
            "code": f"{100_000 + index:06d}",
            "orgId": f"fixture-{100_000 + index:06d}",
            "pinyin": "fixture",
            "zwjc": f"Fixture {100_000 + index}",
        }
        for index in range(999)
    ]
    rows.append(
        {
            "category": "A_SHARE",
            "code": code,
            "orgId": org_id,
            "pinyin": "fixture",
            "zwjc": "Fixture Delisted",
        }
    )
    return json.dumps({"stockList": rows}, ensure_ascii=False).encode("utf-8")


class _SyntheticEvidence:
    def __init__(
        self,
        root: Path,
        *,
        master_root: Path | None = None,
        input_cas_root: Path | None = None,
    ) -> None:
        self.root = root
        self.master_root = master_root or root / "master"
        self.input_cas = input_cas_root or root / "input-cas"
        self.output = root / "audit-output"
        self.master_records = [
            {
                "canonical_entity_id": "CN:SSE:600001",
                "exchange": "SSE",
                "code_alias": "600001.SH",
                "board": "MAIN",
                "listed_at": "2018-01-02",
                "delisted_at": "2018-01-06",
                "valid_from": "2018-01-02",
                "valid_to": "2018-01-06",
                "event_type": "TERMINATED_LISTING",
                "source_url": "https://official.example/security-master",
                "source_hash": "1" * 64,
                "retrieved_at": "2026-08-13T08:00:00+08:00",
                "name": "Synthetic",
                "attributes": {},
            }
        ]
        self.master_identity = self._build_master()
        self.rows = self._base_rows()
        self.index_documents: dict[str, dict[str, Any]] = {}
        self.source_indexes: dict[str, dict[str, Any]] = {}
        self.rebuild_sources()

    def _write_input_cas(self, content: bytes) -> tuple[str, Path]:
        digest = _hash(content)
        path = self.input_cas / "sha256" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return digest, path.resolve()

    def _build_master(self) -> dict[str, Any]:
        master_root = self.master_root
        content = _jsonl(self.master_records)
        content_hash = _hash(content)
        object_path = master_root / "objects" / content_hash[:2] / content_hash
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(content)
        manifest = {
            "protocol_version": "synthetic-security-master-v1",
            "quality_policy_version": "synthetic-v1",
            "sources": [],
            "artifacts": {
                "security_master_jsonl": {
                    "content_hash": content_hash,
                    "object_path": str(object_path.resolve()),
                    "row_count": len(self.master_records),
                }
            },
        }
        manifest_bytes = _canonical(manifest)
        snapshot_id = _hash(manifest_bytes)
        manifest_path = master_root / "manifests" / f"{snapshot_id}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(manifest_bytes)
        return {
            "snapshot_id": snapshot_id,
            "manifest_hash": snapshot_id,
            "manifest_path": str(manifest_path.resolve()),
            "protocol_version": "synthetic-security-master-v1",
        }

    @staticmethod
    def _base_rows() -> dict[str, list[dict[str, Any]]]:
        dates = ["2018-01-02", "2018-01-03", "2018-01-04", "2018-01-05"]
        document_hash = "2" * 64
        timestamps = {
            value: f"{value}T00:00:00+08:00" for value in dates
        }
        raw = [
            {
                "exchange": "SSE",
                "code": "600001.SH",
                "trade_date": value,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.0,
                "volume": 1000.0,
                "amount": 10000.0,
            }
            for value in dates
        ]
        adjusted = [
            {
                "exchange": "SSE",
                "code": "600001.SH",
                "trade_date": value,
                "front_open": 10.0,
                "front_high": 11.0,
                "front_low": 9.0,
                "front_close": 10.0,
                "adjustment_factor": 1.0,
                "anchor_trade_date": "2017-12-29" if index == 0 else None,
                "anchor_adjustment_factor": 1.0 if index == 0 else None,
            }
            for index, value in enumerate(dates)
        ]
        limits = [
            {
                "exchange": "SSE",
                "code": "600001.SH",
                "trade_date": value,
                "limit_up": 11.0,
                "limit_down": 9.0,
                "published_at": timestamps[value],
                "effective_at": timestamps[value],
                "source_document_hash": document_hash,
            }
            for value in dates
        ]
        return {
            "raw_execution_bars": raw,
            "adjusted_bars_factors": adjusted,
            "trading_calendar": [
                {"exchange": "SSE", "trade_date": value, "is_open": True}
                for value in dates
            ],
            "financial_reports": [
                {
                    "exchange": "SSE",
                    "code": "600001.SH",
                    "period_end": "2017-12-31",
                    "report_type": "ANNUAL",
                    "revenue": 100.0,
                    "revenue_yoy": 0.2,
                    "net_profit": 10.0,
                    "net_profit_yoy": 0.3,
                    "gross_margin": 0.4,
                    "roe": 0.1,
                    "operating_cash_flow": 12.0,
                    "published_at": "2018-01-03T18:00:00+08:00",
                    "effective_at": "2018-01-04T00:00:00+08:00",
                    "source_document_hash": document_hash,
                }
            ],
            "earnings_guidance_express": [],
            "gp15_price_limits": limits,
            "gp29_st_status": [
                {
                    "exchange": "SSE",
                    "code": "600001.SH",
                    "valid_from": "2018-01-02",
                    "valid_to": "2018-01-06",
                    "status": "NORMAL",
                    "published_at": "2018-01-01T18:00:00+08:00",
                    "effective_at": "2018-01-02T00:00:00+08:00",
                    "source_document_hash": document_hash,
                }
            ],
            "gp30_corporate_actions": [],
            "gp43_corporate_actions": [],
            "industry_history": [
                {
                    "exchange": "SSE",
                    "code": "600001.SH",
                    "industry_code": "I01",
                    "valid_from": "2018-01-02",
                    "valid_to": "2018-01-06",
                    "published_at": "2018-01-01T18:00:00+08:00",
                    "effective_at": "2018-01-02T00:00:00+08:00",
                    "source_document_hash": document_hash,
                }
            ],
            "announcement_documents": [
                {
                    "exchange": "SSE",
                    "code": "600001.SH",
                    "announcement_id": "A1",
                    "announcement_type": "ANNUAL_REPORT",
                    "published_at": "2018-01-03T18:00:00+08:00",
                    "effective_at": "2018-01-04T00:00:00+08:00",
                    "url": "https://official.example/A1.pdf",
                    "content_hash": document_hash,
                }
            ],
            "suspension_status": [],
        }

    def rebuild_sources(
        self,
        *,
        authorities: Mapping[str, str] | None = None,
        index_mutators: Mapping[str, Callable[[dict[str, Any]], None]] | None = None,
    ) -> None:
        authorities = authorities or {}
        index_mutators = index_mutators or {}
        self.index_documents = {}
        self.source_indexes = {}
        for dataset, contract in DATASET_CONTRACTS.items():
            rows = deepcopy(self.rows[dataset])
            code = "600001.SH" if contract.code_scoped else "*"
            authority = RAW_AUTHORITY_BY_DATASET_EXCHANGE[dataset]["SSE"]
            row_hash_field = ROW_SOURCE_HASH_FIELDS.get(dataset)
            raw_sources: list[dict[str, Any]] = []
            if row_hash_field and rows:
                document_bytes = f"official source document for {dataset}".encode(
                    "utf-8"
                )
                document_hash, document_path = self._write_input_cas(document_bytes)
                for row in rows:
                    row[row_hash_field] = document_hash
                raw_sources.append(
                    {
                        "content_hash": document_hash,
                        "object_path": str(document_path),
                        "byte_count": len(document_bytes),
                        "protocol_version": f"{dataset}-source-document-v1",
                        "authority": authority,
                        "role": "SOURCE_DOCUMENT",
                    }
                )
            content_hash, object_path = self._write_input_cas(_jsonl(rows))
            envelope = {
                "protocol_version": RAW_ENVELOPE_PROTOCOL_VERSION,
                "authority": authority,
                "dataset": dataset,
                "exchange": "SSE",
                "year": 2018,
                "code": code,
                "schema": list(contract.schema),
                "rows": rows,
            }
            envelope_bytes = _canonical(envelope)
            envelope_hash, envelope_path = self._write_input_cas(envelope_bytes)
            raw_sources.insert(
                0,
                {
                    "content_hash": envelope_hash,
                    "object_path": str(envelope_path),
                    "byte_count": len(envelope_bytes),
                    "protocol_version": RAW_ENVELOPE_PROTOCOL_VERSION,
                    "authority": authority,
                    "role": "ROWS_ENVELOPE",
                },
            )
            index = {
                "protocol_version": SOURCE_INDEX_PROTOCOL_VERSION,
                "dataset": dataset,
                "source_protocol_version": contract.source_protocol_version,
                "schema_version": contract.schema_version,
                "schema": list(contract.schema),
                "source_authority": authorities.get(
                    dataset, SOURCE_INDEX_AUTHORITY
                ),
                "coverage_start": AUDIT_START,
                "coverage_end": AUDIT_END,
                "row_count": len(rows),
                "partitions": [
                    {
                        "exchange": "SSE",
                        "year": 2018,
                        "code": code,
                        "query_start": "2018-01-01",
                        "query_end": "2018-12-31",
                        "content_hash": content_hash,
                        "object_path": str(object_path),
                        "row_count": len(rows),
                        "raw_sources": raw_sources,
                    }
                ],
                # These adversarial producer claims must never affect the gate.
                "ready": True,
                "complete": True,
            }
            mutator = index_mutators.get(dataset)
            if mutator:
                mutator(index)
            index_bytes = _canonical(index)
            index_hash, index_path = self._write_input_cas(index_bytes)
            self.index_documents[dataset] = index
            self.source_indexes[dataset] = {
                "content_hash": index_hash,
                "object_path": str(index_path),
                "ready": True,
            }
        self._rebuild_csrc_industry_source()

    def _rebuild_csrc_industry_source(
        self,
        *,
        snapshot_id: str = "TEST_QUALITY_2017Q4",
        published_date: str = "2017-12-31",
    ) -> None:
        spec = csrc_industry.OfficialSnapshotSpec(
            snapshot_id=snapshot_id,
            period_label=f"{snapshot_id} industry classification result",
            page_url=(
                "https://www.csrc.gov.cn/csrc/c100103/"
                f"c{snapshot_id}/content.shtml"
            ),
            pdf_url=(
                "https://www.csrc.gov.cn/csrc/c100103/"
                f"c{snapshot_id}/{snapshot_id}/files/result.pdf"
            ),
            published_date=published_date,
            expected_pdf_sha256=_hash(_SYNTHETIC_INDUSTRY_PDF),
            minimum_assignment_count=2,
        )
        csrc_industry.OFFICIAL_SNAPSHOT_SPECS[snapshot_id] = spec
        page_bytes = (
            "<html><body>"
            f"<h1>{spec.period_label}</h1>"
            f"<p>\u65e5\u671f\uff1a{spec.published_date}</p>"
            f'<a href="{spec.pdf_url}">PDF</a>'
            "</body></html>"
        ).encode("utf-8")
        cas = csrc_industry.CSRCIndustryHistoryCAS(self.input_cas)
        snapshot = csrc_industry._build_snapshot(
            spec=spec,
            page_bytes=page_bytes,
            page_type="text/html",
            pdf_bytes=_SYNTHETIC_INDUSTRY_PDF,
            pdf_type="application/pdf",
            cas=cas,
            retrieved_at="2026-08-13T00:00:00Z",
        )
        manifest = csrc_industry._store_snapshot_manifest(snapshot, cas)
        audit_start = datetime.fromisoformat(AUDIT_START).date()
        audit_end_exclusive = datetime.fromisoformat(AUDIT_END).date() + timedelta(
            days=1
        )
        targets = []
        for row in self.master_records:
            if row["exchange"] not in {"SSE", "SZSE"} or row["delisted_at"] is None:
                continue
            start = max(
                audit_start,
                datetime.fromisoformat(row["listed_at"]).date(),
                datetime.fromisoformat(row["valid_from"]).date(),
            )
            end_exclusive = min(
                audit_end_exclusive,
                datetime.fromisoformat(row["delisted_at"]).date(),
                datetime.fromisoformat(
                    row["valid_to"] or row["delisted_at"]
                ).date(),
            )
            if start >= end_exclusive:
                continue
            targets.append(
                csrc_industry.FrozenIndustryTarget(
                    canonical_entity_id=row["canonical_entity_id"],
                    exchange=row["exchange"],
                    code=row["code_alias"],
                    query_start=start.isoformat(),
                    query_end=(end_exclusive - timedelta(days=1)).isoformat(),
                )
            )
        reference = csrc_industry.build_industry_history_quality_index(
            cas_root=self.input_cas,
            snapshot_manifest_sha256s=(manifest.content_hash,),
            authoritative_master_snapshot_id=self.master_identity["snapshot_id"],
            authoritative_targets=targets,
        )
        self.index_documents["industry_history"] = json.loads(
            Path(reference.object_path).read_text(encoding="utf-8")
        )
        self.source_indexes["industry_history"] = {
            **reference.to_source_identity(),
            "ready": True,
            "complete": True,
        }

    def audit(self, label: str = "case") -> dict[str, Any]:
        return audit_delisted_history(
            master_records=self.master_records,
            master_identity=self.master_identity,
            source_indexes=self.source_indexes,
            input_cas_root=self.input_cas,
            output_root=self.output / label,
        )


class _CninfoQualityEvidence:
    code = "000511.SZ"
    entity_id = "CN:SZSE:000511"
    org_id = "gssz0000511"

    def __init__(
        self,
        root: Path,
        *,
        listed_at: str = "2018-01-01",
        delisted_at: str = "2024-01-01",
        announcement_years: tuple[int, ...] = (2018, 2019, 2020, 2021, 2022, 2023),
    ) -> None:
        self.fixture = _SyntheticEvidence(root)
        self.fixture.master_records = [
            {
                "canonical_entity_id": self.entity_id,
                "exchange": "SZSE",
                "code_alias": self.code,
                "board": "MAIN",
                "listed_at": listed_at,
                "delisted_at": delisted_at,
                "valid_from": listed_at,
                "valid_to": delisted_at,
                "event_type": "TERMINATED_LISTING",
                "source_url": "https://official.example/security-master",
                "source_hash": "1" * 64,
                "retrieved_at": "2026-08-13T08:00:00+08:00",
                "name": "Synthetic SZSE",
                "attributes": {},
            }
        ]
        self.fixture.master_identity = self.fixture._build_master()
        self.fixture.rebuild_sources()
        start = max(datetime.fromisoformat(listed_at).date(), datetime(2018, 1, 1).date())
        end_exclusive = min(
            datetime.fromisoformat(delisted_at).date(),
            datetime(2024, 1, 1).date(),
        )
        self.target = cninfo.FrozenDisclosureTarget(
            canonical_entity_id=self.entity_id,
            exchange="SZSE",
            code=self.code,
            query_start=start.isoformat(),
            query_end=(end_exclusive - timedelta(days=1)).isoformat(),
        )
        rows = [
            _announcement_row(
                f"12{year}000001",
                code=self.code[:6],
                org_id=self.org_id,
                announcement_time=int(
                    datetime(
                        year, 3, 30, tzinfo=ZoneInfo("Asia/Shanghai")
                    ).timestamp()
                    * 1000
                ),
            )
            for year in announcement_years
        ]
        pdfs = {
            str(row["announcementId"]): _pdf(
                f"Official disclosure {row['announcementId']}"
            )
            for row in rows
        }
        disclosure_cas = cninfo.CninfoDisclosureCAS(self.fixture.input_cas)
        disclosure = cninfo.CninfoDelistedDisclosureClient(
            cas=disclosure_cas,
            session=_CninfoSession(
                rows=rows,
                pdfs=pdfs,
                master=_cninfo_stock_master_bytes(self.code[:6], self.org_id),
            ),  # type: ignore[arg-type]
            clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
        ).fetch(
            master_snapshot_id=self.fixture.master_identity["snapshot_id"],
            targets=[self.target],
        )
        self.cninfo_manifest = cninfo.CninfoDelistedDisclosureManifestStore(
            disclosure_cas
        ).seal(disclosure)
        calendar_cas = official_calendar.OfficialTradingCalendarCAS(
            self.fixture.input_cas
        )
        calendar_artifact = official_calendar.OfficialTradingCalendarClient(
            cas=calendar_cas,
            session=_calendar_fixture_session(),
            clock=lambda: CALENDAR_FIXED_NOW,
        ).fetch()
        self.calendar_manifest = (
            official_calendar.OfficialTradingCalendarManifestStore(
                calendar_cas
            ).seal(calendar_artifact)
        )
        self.reference = build_cninfo_announcement_documents_quality_index(
            cas_root=self.fixture.input_cas,
            cninfo_manifest_sha256=self.cninfo_manifest.manifest_sha256,
            calendar_manifest_sha256=self.calendar_manifest.manifest_sha256,
            authoritative_master_snapshot_id=self.fixture.master_identity[
                "snapshot_id"
            ],
            authoritative_targets=[self.target],
        )
        self.fixture.source_indexes["announcement_documents"] = {
            **self.reference.to_source_identity(),
            "ready": True,
        }

    def audit(self, label: str) -> dict[str, Any]:
        return self.fixture.audit(label)

    def rewrite_index(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        content = Path(self.reference.object_path).read_bytes()
        index = json.loads(content)
        mutator(index)
        index_hash, index_path = self.fixture._write_input_cas(_canonical(index))
        self.fixture.source_indexes["announcement_documents"] = {
            "content_hash": index_hash,
            "object_path": str(index_path),
            "ready": True,
        }


class DelistedHistoryQualityTests(unittest.TestCase):
    def test_cninfo_szse_index_is_rebuilt_from_verified_master_and_upstreams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _CninfoQualityEvidence(Path(directory))
            release = evidence.audit("cninfo-integrated")

            counts = release["report"]["gate"]["finding_counts"]
            self.assertNotIn("SOURCE_EVIDENCE_INVALID", counts)
            self.assertFalse(release["report"]["gate"]["ready"])
            self.assertEqual(
                release["report"]["gate"]["status"],
                DELISTED_HISTORY_SOURCE_INCOMPLETE,
            )
            coverage = release["report"]["coverage"][
                "by_dataset_exchange_year_code"
            ]
            announcements = [
                row
                for row in coverage
                if row["dataset"] == "announcement_documents"
            ]
            self.assertEqual(len(announcements), 6)
            self.assertTrue(all(row["exchange"] == "SZSE" for row in announcements))
            self.assertTrue(all(row["covered"] for row in announcements))

    def test_cninfo_master_end_exclusive_becomes_closed_query_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _CninfoQualityEvidence(
                Path(directory),
                delisted_at="2023-07-01",
                announcement_years=(2018, 2019, 2020, 2021, 2022, 2023),
            )
            self.assertEqual(evidence.target.query_end, "2023-06-30")

            release = evidence.audit("cninfo-master-closed-end")

            counts = release["report"]["gate"]["finding_counts"]
            self.assertNotIn("SOURCE_EVIDENCE_INVALID", counts)
            coverage = release["report"]["coverage"][
                "by_dataset_exchange_year_code"
            ]
            self.assertTrue(
                all(
                    row["covered"]
                    for row in coverage
                    if row["dataset"] == "announcement_documents"
                )
            )

    def test_cninfo_empty_year_is_preserved_but_required_rows_still_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _CninfoQualityEvidence(
                Path(directory), announcement_years=(2018, 2019, 2020, 2021, 2022)
            )
            release = evidence.audit("cninfo-empty-year")

            counts = release["report"]["gate"]["finding_counts"]
            self.assertNotIn("SOURCE_EVIDENCE_INVALID", counts)
            self.assertGreaterEqual(counts["REQUIRED_ANNUAL_ROWS_MISSING"], 1)
            row = next(
                item
                for item in release["report"]["coverage"][
                    "by_dataset_exchange_year_code"
                ]
                if item["dataset"] == "announcement_documents"
                and item["exchange"] == "SZSE"
                and item["year"] == 2023
            )
            self.assertTrue(row["covered"])
            self.assertEqual(row["row_count"], 0)

    def test_cninfo_index_rejects_forged_master_scope_and_interval_shift(self) -> None:
        for label, mutation in (
            (
                "scope",
                lambda index: index["upstream_evidence"]["master_scope"].__setitem__(
                    "snapshot_id", "f" * 64
                ),
            ),
            (
                "interval",
                lambda index: index["upstream_evidence"]["master_scope"][
                    "targets"
                ][0].__setitem__("query_end", "2023-12-30"),
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                evidence = _CninfoQualityEvidence(Path(directory))
                evidence.rewrite_index(mutation)
                release = evidence.audit(f"cninfo-{label}")
                self.assertIn(
                    "SOURCE_EVIDENCE_INVALID",
                    release["report"]["gate"]["finding_counts"],
                )

    def test_cninfo_index_rejects_forged_upstream_digest_and_caller_ready(self) -> None:
        for label, mutation in (
            (
                "cninfo-digest",
                lambda index: index["upstream_evidence"]["cninfo"].__setitem__(
                    "logical_content_sha256", "e" * 64
                ),
            ),
            (
                "calendar-digest",
                lambda index: index["upstream_evidence"][
                    "official_trading_calendar"
                ].__setitem__("manifest_sha256", "d" * 64),
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                evidence = _CninfoQualityEvidence(Path(directory))
                evidence.rewrite_index(mutation)
                release = evidence.audit(f"cninfo-{label}")
                self.assertIn(
                    "SOURCE_EVIDENCE_INVALID",
                    release["report"]["gate"]["finding_counts"],
                )

        with tempfile.TemporaryDirectory() as directory:
            evidence = _CninfoQualityEvidence(Path(directory))
            evidence.fixture.source_indexes["announcement_documents"]["ready"] = True
            evidence.fixture.source_indexes["announcement_documents"]["complete"] = True
            release = evidence.audit("cninfo-caller-ready")
            self.assertFalse(release["report"]["gate"]["ready"])
            self.assertTrue(
                release["report"]["gate"][
                    "caller_ready_and_complete_flags_ignored"
                ]
            )

    def test_cninfo_index_rejects_rehashed_rows_upstream_or_sse_impersonation(self) -> None:
        def rewrite_rows(index: dict[str, Any]) -> None:
            partition = index["partitions"][0]
            content = Path(partition["object_path"]).read_bytes()
            rows = [json.loads(line) for line in content.splitlines()]
            rows[0]["announcement_type"] = "FORGED"
            row_hash, row_path = evidence.fixture._write_input_cas(_jsonl(rows))
            partition["content_hash"] = row_hash
            partition["object_path"] = str(row_path)
            envelope = json.loads(
                Path(partition["raw_sources"][0]["object_path"]).read_bytes()
            )
            envelope["rows"] = rows
            envelope_hash, envelope_path = evidence.fixture._write_input_cas(
                _canonical(envelope)
            )
            partition["raw_sources"][0].update(
                {
                    "content_hash": envelope_hash,
                    "object_path": str(envelope_path),
                    "byte_count": len(_canonical(envelope)),
                }
            )

        def rewrite_upstream_summary(index: dict[str, Any]) -> None:
            index["upstream_evidence"]["coverage"][
                "overall_status"
            ] = "READY"
            index["upstream_evidence"]["coverage"]["sse_source_present"] = True
            index["ready"] = True
            index["complete"] = True

        def impersonate_sse(index: dict[str, Any]) -> None:
            for partition in index["partitions"]:
                partition["exchange"] = "SSE"
                partition["code"] = "000511.SH"

        for label, mutation in (
            ("rows", rewrite_rows),
            ("upstream-summary", rewrite_upstream_summary),
            ("sse", impersonate_sse),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                evidence = _CninfoQualityEvidence(Path(directory))
                evidence.rewrite_index(mutation)
                release = evidence.audit(f"cninfo-rehashed-{label}")
                self.assertIn(
                    "SOURCE_EVIDENCE_INVALID",
                    release["report"]["gate"]["finding_counts"],
                )

    def test_cninfo_pdf_hash_is_cold_replayed_not_trusted_from_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _CninfoQualityEvidence(Path(directory))
            content = json.loads(Path(evidence.reference.object_path).read_bytes())
            document = next(
                raw
                for partition in content["partitions"]
                for raw in partition["raw_sources"]
                if raw["role"] == "SOURCE_DOCUMENT"
            )
            Path(document["object_path"]).write_bytes(b"%PDF-forged")
            release = evidence.audit("cninfo-pdf-tampered")
            self.assertIn(
                "SOURCE_EVIDENCE_INVALID",
                release["report"]["gate"]["finding_counts"],
            )

    def test_only_auditor_can_publish_a_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DelistedHistoryQualityCAS(Path(directory))
            self.assertFalse(hasattr(store, "publish"))
            with self.assertRaisesRegex(Exception, "only .*Auditor"):
                store._publish(  # type: ignore[attr-defined]
                    {},
                    master_identity={},
                    source_identities={},
                    input_cas_root=Path(directory),
                    _seal=object(),
                )

    def test_master_records_must_match_the_frozen_manifest_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.master_records[0]["name"] = "caller-tampered"

            with self.assertRaisesRegex(
                DelistedHistoryQualityBlockedError, "do not match"
            ):
                fixture.audit("master-tampered")

    def test_complete_synthetic_evidence_passes_and_publishes_canonical_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            release = fixture.audit("complete")

            self.assertEqual(release["report"]["gate"]["status"], READY)
            self.assertTrue(release["report"]["gate"]["ready"])
            self.assertTrue(
                release["report"]["gate"]["caller_ready_and_complete_flags_ignored"]
            )
            self.assertEqual(
                _hash(Path(release["report_path"]).read_bytes()),
                release["report_hash"],
            )
            self.assertEqual(
                _hash(Path(release["manifest_path"]).read_bytes()),
                release["manifest_hash"],
            )
            code_coverage = release["report"]["coverage"][
                "by_dataset_exchange_year_code"
            ]
            self.assertEqual(len(code_coverage), len(DATASET_CONTRACTS))
            self.assertTrue(all(item["covered"] for item in code_coverage))

    def test_no_source_evidence_is_explicitly_incomplete_despite_caller_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.source_indexes = {}
            release = fixture.audit("missing-all")

            gate = release["report"]["gate"]
            self.assertEqual(gate["status"], DELISTED_HISTORY_SOURCE_INCOMPLETE)
            self.assertFalse(gate["ready"])
            self.assertEqual(
                gate["finding_counts"]["SOURCE_INDEX_MISSING"],
                len(DATASET_CONTRACTS),
            )

    def test_partial_source_manifest_replays_as_source_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.source_indexes = {
                "trading_calendar": fixture.source_indexes["trading_calendar"]
            }
            release = fixture.audit("partial-replay")

            gate = load_verified_delisted_history_gate(
                output_root=fixture.output / "partial-replay",
                input_cas_root=fixture.input_cas,
                security_master_root=fixture.master_root,
                expected_master_gate=fixture.master_identity,
            )

            self.assertEqual(gate["status"], DELISTED_HISTORY_SOURCE_INCOMPLETE)
            self.assertFalse(gate["ready"])
            self.assertTrue(gate["promotion_blocked"])
            self.assertEqual(gate["source_dataset_count"], 1)
            self.assertEqual(
                gate["required_source_dataset_count"], len(DATASET_CONTRACTS)
            )
            self.assertEqual(gate["source_datasets"], ["trading_calendar"])
            self.assertEqual(
                gate["missing_source_datasets"],
                sorted(set(DATASET_CONTRACTS) - {"trading_calendar"}),
            )
            self.assertEqual(
                gate["finding_counts"]["SOURCE_INDEX_MISSING"],
                len(DATASET_CONTRACTS) - 1,
            )
            self.assertEqual(gate["manifest_hash"], release["manifest_hash"])
            self.assertEqual(gate["report_hash"], release["report_hash"])

    def test_complete_source_manifest_replays_as_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            release = fixture.audit("complete-replay")

            gate = load_verified_delisted_history_gate(
                output_root=fixture.output / "complete-replay",
                input_cas_root=fixture.input_cas,
                security_master_root=fixture.master_root,
                expected_master_gate=fixture.master_identity,
            )

            self.assertEqual(gate["status"], READY)
            self.assertTrue(gate["ready"])
            self.assertFalse(gate["promotion_blocked"])
            self.assertEqual(gate["source_dataset_count"], len(DATASET_CONTRACTS))
            self.assertEqual(
                gate["required_source_dataset_count"], len(DATASET_CONTRACTS)
            )
            self.assertEqual(gate["source_datasets"], sorted(DATASET_CONTRACTS))
            self.assertEqual(gate["missing_source_datasets"], [])
            self.assertEqual(gate["finding_counts"], {})
            self.assertEqual(gate["manifest_hash"], release["manifest_hash"])

    def test_partial_source_manifest_cannot_claim_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.source_indexes = {
                "trading_calendar": fixture.source_indexes["trading_calendar"]
            }
            release = fixture.audit("partial-ready-claim")
            output_root = fixture.output / "partial-ready-claim"
            report = deepcopy(release["report"])
            report["gate"].update(
                {
                    "ready": True,
                    "status": READY,
                    "promotion_blocked": False,
                    "hard_failure_count": 0,
                }
            )
            report_bytes = _canonical(report)
            report_hash = _hash(report_bytes)
            report_path = (
                output_root
                / "objects"
                / "sha256"
                / report_hash[:2]
                / report_hash
            )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_bytes(report_bytes)

            manifest = json.loads(Path(release["manifest_path"]).read_bytes())
            manifest["artifacts"]["audit_report"] = {
                "content_hash": report_hash,
                "cas_uri": f"sha256:{report_hash}",
                "byte_count": len(report_bytes),
            }
            manifest_bytes = _canonical(manifest)
            manifest_hash = _hash(manifest_bytes)
            manifest_path = output_root / "manifests" / f"{manifest_hash}.json"
            manifest_path.write_bytes(manifest_bytes)
            pointer = {
                "protocol_version": manifest["protocol_version"],
                "manifest_hash": manifest_hash,
                "manifest_path": str(manifest_path.resolve()),
            }
            (output_root / "current.json").write_bytes(_canonical(pointer))

            gate = load_verified_delisted_history_gate(
                output_root=output_root,
                input_cas_root=fixture.input_cas,
                security_master_root=fixture.master_root,
                expected_master_gate=fixture.master_identity,
            )

            self.assertEqual(gate["status"], "DELISTED_HISTORY_ARTIFACT_INVALID")
            self.assertIn("READY audit manifest", gate["detail"])

    def test_empty_bar_response_needs_independent_suspension_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.rows["raw_execution_bars"] = [
                row
                for row in fixture.rows["raw_execution_bars"]
                if row["trade_date"] != "2018-01-04"
            ]
            fixture.rows["adjusted_bars_factors"] = [
                row
                for row in fixture.rows["adjusted_bars_factors"]
                if row["trade_date"] != "2018-01-04"
            ]
            fixture.rows["gp15_price_limits"] = [
                row
                for row in fixture.rows["gp15_price_limits"]
                if row["trade_date"] != "2018-01-04"
            ]
            fixture.rebuild_sources()
            missing = fixture.audit("missing-bar")
            self.assertIn(
                "RAW_BAR_MISSING_UNEXPLAINED",
                missing["report"]["gate"]["finding_counts"],
            )

            fixture.rows["suspension_status"] = [
                {
                    "exchange": "SSE",
                    "code": "600001.SH",
                    "trade_date": "2018-01-04",
                    "status": "SUSPENDED",
                    "published_at": "2018-01-03T18:00:00+08:00",
                    "effective_at": "2018-01-04T00:00:00+08:00",
                    "source_document_hash": "2" * 64,
                }
            ]
            fixture.rebuild_sources()
            explained = fixture.audit("suspension-explained")
            self.assertEqual(explained["report"]["gate"]["status"], READY)

    def test_all_closed_calendar_cannot_hide_empty_or_unchecked_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            for row in fixture.rows["trading_calendar"]:
                row["is_open"] = False
            fixture.rebuild_sources()

            release = fixture.audit("all-closed-calendar")

            self.assertEqual(
                release["report"]["gate"]["status"],
                DELISTED_HISTORY_SOURCE_INCOMPLETE,
            )
            self.assertIn(
                "TRADING_CALENDAR_SESSION_DENSITY_FAILED",
                release["report"]["gate"]["finding_counts"],
            )

    def test_full_year_suspension_claim_cannot_replace_all_raw_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            dates = [row["trade_date"] for row in fixture.rows["trading_calendar"]]
            fixture.rows["raw_execution_bars"] = []
            fixture.rows["adjusted_bars_factors"] = []
            fixture.rows["gp15_price_limits"] = []
            fixture.rows["suspension_status"] = [
                {
                    "exchange": "SSE",
                    "code": "600001.SH",
                    "trade_date": value,
                    "status": "SUSPENDED",
                    "published_at": f"{value}T00:00:00+08:00",
                    "effective_at": f"{value}T00:00:00+08:00",
                    "source_document_hash": "0" * 64,
                }
                for value in dates
            ]
            fixture.rebuild_sources()

            release = fixture.audit("all-suspended")

            self.assertIn(
                "RAW_SESSION_DENSITY_FAILED",
                release["report"]["gate"]["finding_counts"],
            )
            self.assertFalse(release["report"]["gate"]["ready"])

    def test_normalized_partition_must_replay_from_raw_rows_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            index = deepcopy(fixture.index_documents["raw_execution_bars"])
            partition = index["partitions"][0]
            rows = deepcopy(fixture.rows["raw_execution_bars"])
            rows[0]["close"] = 10.5
            tampered_hash, tampered_path = fixture._write_input_cas(_jsonl(rows))
            partition["content_hash"] = tampered_hash
            partition["object_path"] = str(tampered_path)
            index_bytes = _canonical(index)
            index_hash, index_path = fixture._write_input_cas(index_bytes)
            fixture.source_indexes["raw_execution_bars"] = {
                "content_hash": index_hash,
                "object_path": str(index_path),
            }

            release = fixture.audit("raw-replay-mismatch")

            self.assertIn(
                "SOURCE_EVIDENCE_INVALID",
                release["report"]["gate"]["finding_counts"],
            )
            self.assertFalse(release["report"]["gate"]["ready"])

    def test_post_delisting_financial_and_announcement_rows_do_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            for dataset in ("financial_reports", "announcement_documents"):
                fixture.rows[dataset][0]["published_at"] = (
                    "2018-12-30T18:00:00+08:00"
                )
                fixture.rows[dataset][0]["effective_at"] = (
                    "2018-12-31T00:00:00+08:00"
                )
            fixture.rebuild_sources()

            release = fixture.audit("post-delisting-events")

            self.assertEqual(
                release["report"]["gate"]["finding_counts"].get(
                    "REQUIRED_ANNUAL_ROWS_MISSING"
                ),
                2,
            )
            self.assertFalse(release["report"]["gate"]["ready"])

    def test_2017_factor_anchor_explains_first_2018_corporate_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            for row in fixture.rows["adjusted_bars_factors"]:
                row["front_open"] = 9.0
                row["front_high"] = 9.9
                row["front_low"] = 8.1
                row["front_close"] = 9.0
                row["adjustment_factor"] = 0.9
            action = {
                "exchange": "SSE",
                "code": "600001.SH",
                "event_id": "CA-FIRST",
                "event_type": "CASH_DIVIDEND",
                "ex_date": "2018-01-02",
                "ratio": 0.0,
                "cash_amount": 0.1,
                "published_at": "2018-01-01T18:00:00+08:00",
                "effective_at": "2018-01-02T00:00:00+08:00",
                "source_document_hash": "0" * 64,
            }
            fixture.rows["gp30_corporate_actions"] = [dict(action)]
            fixture.rows["gp43_corporate_actions"] = [dict(action)]
            fixture.rebuild_sources()

            release = fixture.audit("anchored-first-day-action")

            self.assertEqual(release["report"]["gate"]["status"], READY)

    def test_symlinked_input_cas_object_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            identity = fixture.source_indexes["raw_execution_bars"]
            path = Path(identity["object_path"])
            target = path.with_name(path.name + ".target")
            target.write_bytes(path.read_bytes())
            path.unlink()
            try:
                os.symlink(target, path)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            release = fixture.audit("symlinked-cas")

            self.assertIn(
                "SOURCE_EVIDENCE_INVALID",
                release["report"]["gate"]["finding_counts"],
            )

    def test_windows_reparse_output_root_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.output.mkdir(parents=True, exist_ok=True)
            real_lstat = os.lstat

            def marked_reparse(path: Any, *args: Any, **kwargs: Any) -> Any:
                metadata = real_lstat(path, *args, **kwargs)
                if Path(path) == fixture.output:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_file_attributes=(
                            int(getattr(metadata, "st_file_attributes", 0)) | 0x400
                        ),
                    )
                return metadata

            with patch(
                "research_platform.delisted_history_quality.os.lstat",
                side_effect=marked_reparse,
            ):
                with self.assertRaisesRegex(
                    DelistedHistoryQualityBlockedError,
                    "junction, or reparse point",
                ):
                    fixture.audit("reparse-output")

    def test_financial_coverage_cannot_be_satisfied_by_an_empty_partition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.rows["financial_reports"] = []
            fixture.rebuild_sources()
            release = fixture.audit("finance-empty")

            self.assertEqual(
                release["report"]["gate"]["status"],
                DELISTED_HISTORY_SOURCE_INCOMPLETE,
            )
            self.assertIn(
                "REQUIRED_ANNUAL_ROWS_MISSING",
                release["report"]["gate"]["finding_counts"],
            )

    def test_source_index_authority_is_a_frozen_enum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.rebuild_sources(
                authorities={
                    "raw_execution_bars": "same-authority",
                    "suspension_status": "same-authority",
                }
            )
            release = fixture.audit("status-not-independent")

            self.assertEqual(
                release["report"]["gate"]["status"],
                DELISTED_HISTORY_SOURCE_INCOMPLETE,
            )
            self.assertIn(
                "SOURCE_EVIDENCE_INVALID",
                release["report"]["gate"]["finding_counts"],
            )

    def test_raw_source_authority_and_protocol_are_frozen_enums(self) -> None:
        def mutate_raw_authority(index: dict[str, Any]) -> None:
            index["partitions"][0]["raw_sources"][0]["authority"] = "CALLER_TEXT"

        def mutate_raw_protocol(index: dict[str, Any]) -> None:
            index["partitions"][0]["raw_sources"][0]["protocol_version"] = (
                "caller-protocol-v99"
            )

        for label, mutation in (
            ("authority", mutate_raw_authority),
            ("protocol", mutate_raw_protocol),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                fixture = _SyntheticEvidence(Path(directory))
                fixture.rebuild_sources(
                    index_mutators={"raw_execution_bars": mutation}
                )

                release = fixture.audit(f"raw-{label}")

                self.assertIn(
                    "SOURCE_EVIDENCE_INVALID",
                    release["report"]["gate"]["finding_counts"],
                )
                self.assertFalse(release["report"]["gate"]["ready"])

    def test_gp30_and_gp43_corporate_actions_must_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.rows["gp30_corporate_actions"] = [
                {
                    "exchange": "SSE",
                    "code": "600001.SH",
                    "event_id": "CA1",
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "2018-01-04",
                    "ratio": 0.0,
                    "cash_amount": 0.1,
                    "published_at": "2018-01-03T18:00:00+08:00",
                    "effective_at": "2018-01-04T00:00:00+08:00",
                    "source_document_hash": "3" * 64,
                }
            ]
            fixture.rebuild_sources()
            release = fixture.audit("corp-disagreement")

            self.assertIn(
                "CORPORATE_ACTION_SOURCES_DISAGREE",
                release["report"]["gate"]["finding_counts"],
            )
            self.assertFalse(release["report"]["gate"]["ready"])

    def test_hash_row_count_and_schema_drift_fail_closed(self) -> None:
        cases: list[tuple[str, Callable[[_SyntheticEvidence], None]]] = []

        def corrupt_hash(fixture: _SyntheticEvidence) -> None:
            path = Path(
                fixture.source_indexes["raw_execution_bars"]["object_path"]
            )
            path.write_bytes(b"tampered")

        def row_count_drift(fixture: _SyntheticEvidence) -> None:
            fixture.rebuild_sources(
                index_mutators={
                    "raw_execution_bars": lambda index: index.__setitem__(
                        "row_count", index["row_count"] + 1
                    )
                }
            )

        def schema_drift(fixture: _SyntheticEvidence) -> None:
            fixture.rebuild_sources(
                index_mutators={
                    "raw_execution_bars": lambda index: index["schema"].append(
                        "unexpected"
                    )
                }
            )

        cases.extend(
            [
                ("hash", corrupt_hash),
                ("row-count", row_count_drift),
                ("schema", schema_drift),
            ]
        )
        for label, mutation in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                fixture = _SyntheticEvidence(Path(directory))
                mutation(fixture)
                release = fixture.audit(label)
                self.assertIn(
                    "SOURCE_EVIDENCE_INVALID",
                    release["report"]["gate"]["finding_counts"],
                )
                self.assertFalse(release["report"]["gate"]["ready"])

    def test_financial_timepoint_violation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.rows["financial_reports"][0]["effective_at"] = (
                "2018-01-02T00:00:00+08:00"
            )
            fixture.rebuild_sources()
            release = fixture.audit("timepoint")

            self.assertIn(
                "POINT_IN_TIME_FIELDS_INVALID",
                release["report"]["gate"]["finding_counts"],
            )
            self.assertFalse(release["report"]["gate"]["ready"])

    def test_calendar_must_explain_every_weekday_in_listing_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.rows["trading_calendar"] = [
                row
                for row in fixture.rows["trading_calendar"]
                if row["trade_date"] != "2018-01-04"
            ]
            fixture.rebuild_sources()
            release = fixture.audit("calendar-gap")

            self.assertEqual(
                release["report"]["gate"]["status"],
                DELISTED_HISTORY_SOURCE_INCOMPLETE,
            )
            self.assertIn(
                "TRADING_CALENDAR_DATE_MISSING",
                release["report"]["gate"]["finding_counts"],
            )

    def test_bar_must_be_unique_inside_listing_interval_and_open_session(self) -> None:
        cases: list[tuple[str, Callable[[_SyntheticEvidence], None], str]] = []

        def duplicate_row(fixture: _SyntheticEvidence) -> None:
            fixture.rows["raw_execution_bars"].append(
                deepcopy(fixture.rows["raw_execution_bars"][0])
            )

        def closed_session(fixture: _SyntheticEvidence) -> None:
            fixture.rows["trading_calendar"][2]["is_open"] = False

        def outside_interval(fixture: _SyntheticEvidence) -> None:
            fixture.rows["raw_execution_bars"].append(
                {
                    **fixture.rows["raw_execution_bars"][-1],
                    "trade_date": "2018-01-08",
                }
            )

        cases.extend(
            [
                ("duplicate", duplicate_row, "SOURCE_EVIDENCE_INVALID"),
                ("closed-session", closed_session, "BAR_NOT_MARKET_SESSION"),
                (
                    "outside-listing-interval",
                    outside_interval,
                    "BAR_OUTSIDE_LISTING_INTERVAL",
                ),
            ]
        )
        for label, mutation, expected_finding in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                fixture = _SyntheticEvidence(Path(directory))
                mutation(fixture)
                fixture.rebuild_sources()

                release = fixture.audit(f"bar-{label}")

                self.assertIn(
                    expected_finding,
                    release["report"]["gate"]["finding_counts"],
                )
                self.assertFalse(release["report"]["gate"]["ready"])

    def test_raw_front_factor_crosscheck_is_a_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _SyntheticEvidence(Path(directory))
            fixture.rows["adjusted_bars_factors"][0]["front_close"] = 99.0
            fixture.rebuild_sources()
            release = fixture.audit("factor-crosscheck")

            self.assertIn(
                "ADJUSTMENT_CROSSCHECK_FAILED",
                release["report"]["gate"]["finding_counts"],
            )
            self.assertEqual(
                release["report"]["gate"]["status"],
                DELISTED_HISTORY_QUALITY_REJECTED,
            )


if __name__ == "__main__":
    unittest.main()
