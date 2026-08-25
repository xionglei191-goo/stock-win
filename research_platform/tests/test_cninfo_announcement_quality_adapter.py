from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from research_platform import cninfo_delisted_disclosures as cninfo
from research_platform import official_trading_calendar as calendar
from research_platform.cninfo_announcement_quality_adapter import (
    CninfoAnnouncementQualityAdapterBlockedError,
    OVERALL_STATUS,
    PROTOCOL_VERSION,
    UPSTREAM_EVIDENCE_KIND,
    build_cninfo_announcement_documents_quality_index,
    replay_cninfo_announcement_documents_quality_index,
)
from research_platform.tests.test_cninfo_delisted_disclosures import (
    _Session,
    _announcement_row,
    _pdf,
)
from research_platform.tests.test_official_trading_calendar import (
    FIXED_NOW,
    _fixture_session,
)


_CHINA = ZoneInfo("Asia/Shanghai")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _master_bytes(code: str, org_id: str) -> bytes:
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


class CninfoAnnouncementQualityAdapterTests(unittest.TestCase):
    snapshot_id = "a" * 64
    target = cninfo.FrozenDisclosureTarget(
        canonical_entity_id="CN:SZSE:000511",
        exchange="SZSE",
        code="000511.SZ",
        query_start="2018-01-01",
        query_end="2023-12-31",
    )
    org_id = "gssz0000511"

    def _build_upstreams(
        self,
        root: Path,
        *,
        rows: list[dict[str, object]] | None = None,
        pdfs: dict[str, bytes] | None = None,
        target: cninfo.FrozenDisclosureTarget | None = None,
    ):
        observed_target = target or self.target
        observed_rows = rows or [
            _announcement_row(
                "1200000001",
                code=observed_target.code[:6],
                org_id=self.org_id,
            )
        ]
        observed_pdfs = pdfs or {
            str(row["announcementId"]): _pdf("Official disclosure")
            for row in observed_rows
        }
        disclosure_cas = cninfo.CninfoDisclosureCAS(root)
        disclosure = cninfo.CninfoDelistedDisclosureClient(
            cas=disclosure_cas,
            session=_Session(
                rows=observed_rows,
                pdfs=observed_pdfs,
                master=_master_bytes(observed_target.code[:6], self.org_id),
            ),  # type: ignore[arg-type]
            clock=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
        ).fetch(
            master_snapshot_id=self.snapshot_id,
            targets=[observed_target],
        )
        disclosure_manifest = cninfo.CninfoDelistedDisclosureManifestStore(
            disclosure_cas
        ).seal(disclosure)

        calendar_cas = calendar.OfficialTradingCalendarCAS(root)
        calendar_artifact = calendar.OfficialTradingCalendarClient(
            cas=calendar_cas,
            session=_fixture_session(),
            clock=lambda: FIXED_NOW,
        ).fetch()
        calendar_manifest = calendar.OfficialTradingCalendarManifestStore(
            calendar_cas
        ).seal(calendar_artifact)
        return (
            disclosure_cas,
            disclosure,
            disclosure_manifest,
            calendar_artifact,
            calendar_manifest,
        )

    def _build_index(self, root: Path, **kwargs: object):
        upstreams = self._build_upstreams(root, **kwargs)
        target = kwargs.get("target") or self.target
        reference = build_cninfo_announcement_documents_quality_index(
            cas_root=root,
            cninfo_manifest_sha256=upstreams[2].manifest_sha256,
            calendar_manifest_sha256=upstreams[4].manifest_sha256,
            authoritative_master_snapshot_id=self.snapshot_id,
            authoritative_targets=[target],  # type: ignore[list-item]
        )
        return (*upstreams, reference)

    def test_builds_szse_only_partitions_and_never_promotes_overall_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, disclosure, cninfo_manifest, calendar_artifact, calendar_manifest, reference = (
                self._build_index(root)
            )
            content, _ = cas.read_blob(reference.content_hash)
            index = json.loads(content)

            self.assertEqual(reference.partition_count, 6)
            self.assertEqual(reference.row_count, 1)
            self.assertEqual(reference.empty_partition_count, 5)
            self.assertFalse(reference.szse_required_annual_coverage_complete)
            self.assertFalse(reference.ready)
            self.assertFalse(reference.complete)
            self.assertEqual(reference.overall_status, OVERALL_STATUS)
            self.assertFalse(index["ready"])
            self.assertFalse(index["complete"])
            self.assertEqual(
                index["upstream_evidence"]["coverage"]["overall_status"],
                OVERALL_STATUS,
            )
            self.assertEqual(
                index["upstream_evidence"]["kind"],
                "CNINFO_SZSE_ANNOUNCEMENTS_WITH_OFFICIAL_CALENDAR_V1",
            )
            self.assertEqual(
                index["upstream_evidence"]["adapter_protocol_version"],
                "cninfo-announcement-documents-quality-adapter-v2",
            )
            self.assertEqual(
                index["upstream_evidence"]["kind"], UPSTREAM_EVIDENCE_KIND
            )
            self.assertEqual(
                index["upstream_evidence"]["adapter_protocol_version"],
                PROTOCOL_VERSION,
            )
            self.assertFalse(
                index["upstream_evidence"]["coverage"]["sse_source_present"]
            )
            self.assertEqual(
                index["upstream_evidence"]["master_scope"]["snapshot_id"],
                self.snapshot_id,
            )
            self.assertFalse(
                index["upstream_evidence"]["master_scope"][
                    "caller_ready_accepted"
                ]
            )
            self.assertEqual(
                index["upstream_evidence"]["cninfo"]["manifest_sha256"],
                cninfo_manifest.manifest_sha256,
            )
            self.assertEqual(
                index["upstream_evidence"]["official_trading_calendar"][
                    "manifest_sha256"
                ],
                calendar_manifest.manifest_sha256,
            )
            self.assertEqual(
                index["upstream_evidence"]["cninfo"][
                    "logical_content_sha256"
                ],
                disclosure.logical_content_sha256,
            )
            self.assertEqual(
                index["upstream_evidence"]["official_trading_calendar"][
                    "logical_content_sha256"
                ],
                calendar_artifact.logical_content_sha256,
            )
            for partition in index["partitions"]:
                self.assertEqual(partition["exchange"], "SZSE")
                self.assertEqual(partition["code"], "000511.SZ")
                self.assertEqual(
                    partition["query_start"], f"{partition['year']}-01-01"
                )
                self.assertEqual(
                    partition["query_end"], f"{partition['year']}-12-31"
                )
            replayed = replay_cninfo_announcement_documents_quality_index(
                cas_root=root,
                source_index_sha256=reference.content_hash,
                cninfo_manifest_sha256=cninfo_manifest.manifest_sha256,
                calendar_manifest_sha256=calendar_manifest.manifest_sha256,
                authoritative_master_snapshot_id=self.snapshot_id,
                authoritative_targets=[self.target],
            )
            self.assertEqual(replayed, reference)

    def test_midyear_listing_limits_partitions_and_binds_actual_scope(self) -> None:
        target = cninfo.FrozenDisclosureTarget(
            canonical_entity_id="CN:SZSE:000511",
            exchange="SZSE",
            code="000511.SZ",
            query_start="2019-07-22",
            query_end="2023-12-31",
        )
        listed_at = datetime(2019, 7, 22, 10, 0, tzinfo=_CHINA)
        rows = [
            _announcement_row(
                "1200000003",
                code="000511",
                org_id=self.org_id,
                announcement_time=int(listed_at.timestamp() * 1000),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _disclosure, _cm, _ca, _tm, reference = self._build_index(
                root,
                target=target,
                rows=rows,
                pdfs={"1200000003": _pdf("Official disclosure")},
            )
            content, _ = cas.read_blob(reference.content_hash)
            index = json.loads(content)

            self.assertEqual(reference.partition_count, 5)
            self.assertEqual(
                [partition["year"] for partition in index["partitions"]],
                [2019, 2020, 2021, 2022, 2023],
            )
            scope = index["upstream_evidence"]["master_scope"]
            self.assertEqual(scope["targets"], [target.to_dict()])
            self.assertEqual(
                scope["target_interval_semantics"], "CLOSED_START_AND_END"
            )
            self.assertEqual(reference.master_scope_sha256, scope["scope_sha256"])
            expected_scope_hash = hashlib.sha256(
                _canonical(
                    {
                        "snapshot_id": self.snapshot_id,
                        "targets": [target.to_dict()],
                    }
                )
            ).hexdigest()
            self.assertEqual(reference.master_scope_sha256, expected_scope_hash)

    def test_midyear_delisting_limits_partitions_and_accepts_closed_end(self) -> None:
        target = cninfo.FrozenDisclosureTarget(
            canonical_entity_id="CN:SZSE:000511",
            exchange="SZSE",
            code="000511.SZ",
            query_start="2018-01-01",
            query_end="2021-06-18",
        )
        delisted_at = datetime(2021, 6, 18, 14, 30, tzinfo=_CHINA)
        rows = [
            _announcement_row(
                "1200000004",
                code="000511",
                org_id=self.org_id,
                announcement_time=int(delisted_at.timestamp() * 1000),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _disclosure, _cm, _ca, _tm, reference = self._build_index(
                root,
                target=target,
                rows=rows,
                pdfs={"1200000004": _pdf("Official disclosure")},
            )
            content, _ = cas.read_blob(reference.content_hash)
            index = json.loads(content)

            self.assertEqual(reference.partition_count, 4)
            self.assertEqual(
                [partition["year"] for partition in index["partitions"]],
                [2018, 2019, 2020, 2021],
            )
            last_partition = index["partitions"][-1]
            rows_bytes, _ = cas.read_blob(last_partition["content_hash"])
            emitted = json.loads(rows_bytes.splitlines()[0])
            self.assertEqual(
                emitted["effective_at"], "2021-06-18T14:30:00+08:00"
            )

    def test_announcement_outside_actual_target_interval_fails_closed(self) -> None:
        target = cninfo.FrozenDisclosureTarget(
            canonical_entity_id="CN:SZSE:000511",
            exchange="SZSE",
            code="000511.SZ",
            query_start="2019-07-22",
            query_end="2021-06-18",
        )
        cases = (
            ("1200000005", datetime(2019, 7, 19, 10, 0, tzinfo=_CHINA)),
            ("1200000006", datetime(2021, 6, 21, 10, 0, tzinfo=_CHINA)),
        )
        for announcement_id, published_at in cases:
            with self.subTest(announcement_id=announcement_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = [
                    _announcement_row(
                        announcement_id,
                        code="000511",
                        org_id=self.org_id,
                        announcement_time=int(published_at.timestamp() * 1000),
                    )
                ]
                upstreams = self._build_upstreams(
                    root,
                    target=target,
                    rows=rows,
                    pdfs={announcement_id: _pdf("Official disclosure")},
                )
                with self.assertRaisesRegex(
                    CninfoAnnouncementQualityAdapterBlockedError,
                    "published_at falls outside authoritative target interval",
                ):
                    build_cninfo_announcement_documents_quality_index(
                        cas_root=root,
                        cninfo_manifest_sha256=upstreams[2].manifest_sha256,
                        calendar_manifest_sha256=upstreams[4].manifest_sha256,
                        authoritative_master_snapshot_id=self.snapshot_id,
                        authoritative_targets=[target],
                    )

    def test_after_close_on_delisting_date_cannot_effect_after_target_end(self) -> None:
        target = cninfo.FrozenDisclosureTarget(
            canonical_entity_id="CN:SZSE:000511",
            exchange="SZSE",
            code="000511.SZ",
            query_start="2019-07-22",
            query_end="2021-06-18",
        )
        published_at = datetime(2021, 6, 18, 15, 1, tzinfo=_CHINA)
        rows = [
            _announcement_row(
                "1200000007",
                code="000511",
                org_id=self.org_id,
                announcement_time=int(published_at.timestamp() * 1000),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstreams = self._build_upstreams(
                root,
                target=target,
                rows=rows,
                pdfs={"1200000007": _pdf("Official disclosure")},
            )
            with self.assertRaisesRegex(
                CninfoAnnouncementQualityAdapterBlockedError,
                "effective_at falls outside authoritative target interval",
            ):
                build_cninfo_announcement_documents_quality_index(
                    cas_root=root,
                    cninfo_manifest_sha256=upstreams[2].manifest_sha256,
                    calendar_manifest_sha256=upstreams[4].manifest_sha256,
                    authoritative_master_snapshot_id=self.snapshot_id,
                    authoritative_targets=[target],
                )

    def test_effective_at_uses_official_sessions_and_conservative_close_rule(
        self,
    ) -> None:
        date_only_weekend = datetime(2022, 3, 26, tzinfo=_CHINA)
        pre_close = datetime(2022, 3, 25, 14, 59, tzinfo=_CHINA)
        after_close = datetime(2022, 3, 25, 15, 1, tzinfo=_CHINA)
        rows = [
            _announcement_row(
                "1200000010",
                code="000511",
                org_id=self.org_id,
                announcement_time=int(date_only_weekend.timestamp() * 1000),
            ),
            _announcement_row(
                "1200000011",
                code="000511",
                org_id=self.org_id,
                announcement_time=int(pre_close.timestamp() * 1000),
            ),
            _announcement_row(
                "1200000012",
                code="000511",
                org_id=self.org_id,
                announcement_time=int(after_close.timestamp() * 1000),
            ),
        ]
        pdfs = {
            str(row["announcementId"]): _pdf("Official disclosure")
            for row in rows
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _disclosure, _cm, _ca, _tm, reference = self._build_index(
                root, rows=rows, pdfs=pdfs
            )
            content, _ = cas.read_blob(reference.content_hash)
            index = json.loads(content)
            partition = next(
                item for item in index["partitions"] if item["year"] == 2022
            )
            rows_bytes, _ = cas.read_blob(partition["content_hash"])
            normalized = {
                row["announcement_id"]: row
                for row in (
                    json.loads(line) for line in rows_bytes.splitlines()
                )
            }

            self.assertEqual(
                normalized["1200000010"]["published_at"],
                "2022-03-26T00:00:00+08:00",
            )
            self.assertEqual(
                normalized["1200000010"]["effective_at"],
                "2022-03-28T00:00:00+08:00",
            )
            self.assertEqual(
                normalized["1200000011"]["effective_at"],
                "2022-03-25T14:59:00+08:00",
            )
            self.assertEqual(
                normalized["1200000012"]["effective_at"],
                "2022-03-28T00:00:00+08:00",
            )

    def test_only_emits_announcement_rows_not_structured_financial_values(self) -> None:
        rows = [
            _announcement_row(
                "1200000020",
                code="000511",
                org_id=self.org_id,
                title="2022 Annual Report",
            ),
            _announcement_row(
                "1200000021",
                code="000511",
                org_id=self.org_id,
                title="2022 Annual Earnings Forecast",
                announcement_time=int(
                    datetime(2022, 4, 1, tzinfo=_CHINA).timestamp() * 1000
                ),
            ),
        ]
        pdfs = {
            "1200000020": _pdf("2022 Annual Report"),
            "1200000021": _pdf("2022 Annual Earnings Forecast"),
        }
        forbidden_fields = {
            "revenue",
            "net_profit",
            "forecast_low",
            "forecast_high",
            "previous_value",
            "period_end",
            "report_type",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, disclosure, _cm, _ca, _tm, reference = self._build_index(
                root, rows=rows, pdfs=pdfs
            )
            self.assertEqual(len(disclosure.classification_candidates), 2)
            content, _ = cas.read_blob(reference.content_hash)
            index = json.loads(content)
            self.assertEqual(index["dataset"], "announcement_documents")
            self.assertEqual(
                index["schema"],
                [
                    "exchange",
                    "code",
                    "announcement_id",
                    "announcement_type",
                    "published_at",
                    "effective_at",
                    "url",
                    "content_hash",
                ],
            )
            for partition in index["partitions"]:
                rows_bytes, _ = cas.read_blob(partition["content_hash"])
                for line in rows_bytes.splitlines():
                    row = json.loads(line)
                    self.assertTrue(forbidden_fields.isdisjoint(row))
            serialized = json.dumps(index, ensure_ascii=False)
            self.assertNotIn('"financial_reports"', serialized)
            self.assertNotIn('"earnings_guidance_express"', serialized)

    def test_wrong_master_snapshot_or_scope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cas, _artifact, disclosure_manifest, _ca, calendar_manifest = (
                self._build_upstreams(root)
            )
            with self.assertRaisesRegex(
                CninfoAnnouncementQualityAdapterBlockedError,
                "master_snapshot_id",
            ):
                build_cninfo_announcement_documents_quality_index(
                    cas_root=root,
                    cninfo_manifest_sha256=disclosure_manifest.manifest_sha256,
                    calendar_manifest_sha256=calendar_manifest.manifest_sha256,
                    authoritative_master_snapshot_id="b" * 64,
                    authoritative_targets=[self.target],
                )
            wrong_scope = cninfo.FrozenDisclosureTarget(
                canonical_entity_id="CN:SZSE:000512",
                exchange="SZSE",
                code="000512.SZ",
                query_start="2018-01-01",
                query_end="2023-12-31",
            )
            with self.assertRaisesRegex(
                CninfoAnnouncementQualityAdapterBlockedError,
                "targets do not match",
            ):
                build_cninfo_announcement_documents_quality_index(
                    cas_root=root,
                    cninfo_manifest_sha256=disclosure_manifest.manifest_sha256,
                    calendar_manifest_sha256=calendar_manifest.manifest_sha256,
                    authoritative_master_snapshot_id=self.snapshot_id,
                    authoritative_targets=[wrong_scope],
                )

    def test_tampered_cninfo_pdf_or_calendar_manifest_fails_cold_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cas, disclosure, disclosure_manifest, _ca, calendar_manifest = (
                self._build_upstreams(root)
            )
            pdf_path = Path(disclosure.documents[0]["raw"]["object_path"])
            pdf_path.write_bytes(pdf_path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                CninfoAnnouncementQualityAdapterBlockedError,
                "CNINFO manifest failed cold replay",
            ):
                build_cninfo_announcement_documents_quality_index(
                    cas_root=root,
                    cninfo_manifest_sha256=disclosure_manifest.manifest_sha256,
                    calendar_manifest_sha256=calendar_manifest.manifest_sha256,
                    authoritative_master_snapshot_id=self.snapshot_id,
                    authoritative_targets=[self.target],
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cas, _disclosure, disclosure_manifest, _ca, calendar_manifest = (
                self._build_upstreams(root)
            )
            calendar_path = Path(calendar_manifest.object_path)
            calendar_path.write_bytes(calendar_path.read_bytes() + b" ")
            with self.assertRaisesRegex(
                CninfoAnnouncementQualityAdapterBlockedError,
                "official calendar manifest failed cold replay",
            ):
                build_cninfo_announcement_documents_quality_index(
                    cas_root=root,
                    cninfo_manifest_sha256=disclosure_manifest.manifest_sha256,
                    calendar_manifest_sha256=calendar_manifest.manifest_sha256,
                    authoritative_master_snapshot_id=self.snapshot_id,
                    authoritative_targets=[self.target],
                )

    def test_consistently_forged_index_is_rejected_by_upstream_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _disclosure, disclosure_manifest, _ca, calendar_manifest, reference = (
                self._build_index(root)
            )
            content, _ = cas.read_blob(reference.content_hash)
            index = json.loads(content)
            index["ready"] = True
            forged_bytes = _canonical(index)
            forged_hash, _ = cas.put_blob(forged_bytes)

            with self.assertRaisesRegex(
                CninfoAnnouncementQualityAdapterBlockedError,
                "does not cold replay exactly",
            ):
                replay_cninfo_announcement_documents_quality_index(
                    cas_root=root,
                    source_index_sha256=forged_hash,
                    cninfo_manifest_sha256=disclosure_manifest.manifest_sha256,
                    calendar_manifest_sha256=calendar_manifest.manifest_sha256,
                    authoritative_master_snapshot_id=self.snapshot_id,
                    authoritative_targets=[self.target],
                )


if __name__ == "__main__":
    unittest.main()
