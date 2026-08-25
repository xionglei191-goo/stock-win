from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from research_platform import csrc_industry_history_source as source


FIXTURE_ASSIGNMENTS = (
    source.IndustryAssignment("SZSE", "000511.SZ", "C30"),
    source.IndustryAssignment("SSE", "600432.SH", "C32"),
)


def fake_extraction(_content: bytes):
    return FIXTURE_ASSIGNMENTS, "pypdf", "TEST", 1, "1" * 64


def page_bytes(spec: source.OfficialSnapshotSpec) -> bytes:
    return (
        "<html><body>"
        f"<h1>{spec.period_label}</h1>"
        f"<p>日期：{spec.published_date}</p>"
        f'<a href="{spec.pdf_url}">PDF</a>'
        "</body></html>"
    ).encode("utf-8")


def install_fixture_spec(
    snapshot_id: str,
    *,
    published_date: str,
    pdf: bytes,
) -> source.OfficialSnapshotSpec:
    spec = source.OfficialSnapshotSpec(
        snapshot_id=snapshot_id,
        period_label=f"{snapshot_id}上市公司行业分类结果",
        page_url=f"https://www.csrc.gov.cn/csrc/c100103/c{snapshot_id}/content.shtml",
        pdf_url=(
            f"https://www.csrc.gov.cn/csrc/c100103/c{snapshot_id}/"
            f"{snapshot_id}/files/result.pdf"
        ),
        published_date=published_date,
        expected_pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        minimum_assignment_count=2,
    )
    source.OFFICIAL_SNAPSHOT_SPECS[snapshot_id] = spec
    return spec


class FakeResponse:
    def __init__(self, *, url: str, content: bytes, content_type: str) -> None:
        self.status_code = 200
        self.url = url
        self.content = content
        self.headers = {"Content-Type": content_type}


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.urls.append(url)
        return self.responses[url]


class CSRCIndustryHistorySourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_specs = dict(source.OFFICIAL_SNAPSHOT_SPECS)
        self.pdf_2017 = b"%PDF fixture 2017Q4"
        self.pdf_2018 = b"%PDF fixture 2018Q1"
        self.spec_2017 = install_fixture_spec(
            "TEST2017Q4", published_date="2018-01-19", pdf=self.pdf_2017
        )
        self.spec_2018 = install_fixture_spec(
            "TEST2018Q1", published_date="2018-05-21", pdf=self.pdf_2018
        )

    def tearDown(self) -> None:
        source.OFFICIAL_SNAPSHOT_SPECS.clear()
        source.OFFICIAL_SNAPSHOT_SPECS.update(self.original_specs)

    def _capture(self, root: Path, spec: source.OfficialSnapshotSpec, pdf: bytes):
        responses = {
            spec.page_url: FakeResponse(
                url=spec.page_url,
                content=page_bytes(spec),
                content_type="text/html",
            ),
            spec.pdf_url: FakeResponse(
                url=spec.pdf_url,
                content=pdf,
                content_type="application/pdf",
            ),
        }
        with patch.object(source, "_extract_pdf_assignments", side_effect=fake_extraction):
            return source.capture_official_industry_snapshot(
                cas_root=root,
                snapshot_id=spec.snapshot_id,
                session=FakeSession(responses),
                retrieved_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
            )

    def test_capture_and_cold_replay_bind_page_pdf_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = self._capture(root, self.spec_2017, self.pdf_2017)
            self.assertFalse(reference.ready)
            self.assertEqual(reference.assignment_count, 2)
            with patch.object(source, "_extract_pdf_assignments", side_effect=fake_extraction):
                replayed = source.replay_official_industry_snapshot(
                    cas_root=root, manifest_sha256=reference.content_hash
                )
            self.assertEqual(replayed.available_from, "2018-01-20")
            self.assertEqual(
                {item.code: item.industry_code for item in replayed.assignments},
                {"000511.SZ": "C30", "600432.SH": "C32"},
            )
            self.assertFalse(replayed.source_contract["ready"])
            self.assertFalse(replayed.source_contract["training_allowed"])

    def test_page_date_or_pdf_hash_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad_page = page_bytes(self.spec_2017).replace(b"2018-01-19", b"2018-01-18")
            responses = {
                self.spec_2017.page_url: FakeResponse(
                    url=self.spec_2017.page_url,
                    content=bad_page,
                    content_type="text/html",
                ),
                self.spec_2017.pdf_url: FakeResponse(
                    url=self.spec_2017.pdf_url,
                    content=self.pdf_2017,
                    content_type="application/pdf",
                ),
            }
            with self.assertRaisesRegex(
                source.CSRCIndustryHistoryBlockedError, "publication date"
            ):
                source.capture_official_industry_snapshot(
                    cas_root=root,
                    snapshot_id=self.spec_2017.snapshot_id,
                    session=FakeSession(responses),
                )
            responses[self.spec_2017.page_url] = FakeResponse(
                url=self.spec_2017.page_url,
                content=page_bytes(self.spec_2017),
                content_type="text/html",
            )
            responses[self.spec_2017.pdf_url] = FakeResponse(
                url=self.spec_2017.pdf_url,
                content=self.pdf_2017 + b"tampered",
                content_type="application/pdf",
            )
            with self.assertRaisesRegex(
                source.CSRCIndustryHistoryBlockedError, "PDF hash"
            ):
                source.capture_official_industry_snapshot(
                    cas_root=root,
                    snapshot_id=self.spec_2017.snapshot_id,
                    session=FakeSession(responses),
                )

    def test_quality_index_uses_only_information_available_at_target_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._capture(root, self.spec_2017, self.pdf_2017)
            second = self._capture(root, self.spec_2018, self.pdf_2018)
            targets = (
                source.FrozenIndustryTarget(
                    "sse-600432", "SSE", "600432.SH", "2018-01-01", "2018-06-30"
                ),
                source.FrozenIndustryTarget(
                    "szse-000511", "SZSE", "000511.SZ", "2018-01-01", "2018-06-30"
                ),
            )
            with patch.object(source, "_extract_pdf_assignments", side_effect=fake_extraction):
                reference = source.build_industry_history_quality_index(
                    cas_root=root,
                    snapshot_manifest_sha256s=(first.content_hash, second.content_hash),
                    authoritative_master_snapshot_id="a" * 64,
                    authoritative_targets=targets,
                )
            self.assertFalse(reference.ready)
            self.assertFalse(reference.complete)
            self.assertEqual(reference.evidence_target_count, 2)
            self.assertEqual(reference.covered_target_count, 0)
            index = json.loads(Path(reference.object_path).read_text(encoding="utf-8"))
            self.assertEqual(index["upstream_evidence"]["kind"], source.UPSTREAM_EVIDENCE_KIND)
            self.assertEqual(
                index["upstream_evidence"]["integration_contract"][
                    "required_exchange_raw_authorities"
                ],
                {
                    "SSE": source.OFFICIAL_INDUSTRY_RAW_AUTHORITY,
                    "SZSE": source.OFFICIAL_INDUSTRY_RAW_AUTHORITY,
                },
            )
            self.assertFalse(index["upstream_evidence"]["master_scope"]["all_targets_covered"])
            rows = []
            for partition in index["partitions"]:
                content = Path(partition["object_path"]).read_text(encoding="utf-8")
                rows.extend(json.loads(line) for line in content.splitlines())
            self.assertEqual({row["valid_from"] for row in rows}, {"2018-01-20"})
            self.assertNotIn("2018-01-01", {row["valid_from"] for row in rows})

    def test_baseline_snapshot_builds_continuous_rows_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._capture(root, self.spec_2017, self.pdf_2017)
            targets = (
                source.FrozenIndustryTarget(
                    "sse-600432", "SSE", "600432.SH", "2018-01-20", "2018-06-30"
                ),
                source.FrozenIndustryTarget(
                    "szse-000511", "SZSE", "000511.SZ", "2018-01-20", "2018-06-30"
                ),
            )
            with patch.object(source, "_extract_pdf_assignments", side_effect=fake_extraction):
                reference = source.build_industry_history_quality_index(
                    cas_root=root,
                    snapshot_manifest_sha256s=(first.content_hash,),
                    authoritative_master_snapshot_id="b" * 64,
                    authoritative_targets=targets,
                )
                replayed = source.replay_industry_history_quality_index(
                    cas_root=root,
                    source_index_sha256=reference.content_hash,
                    snapshot_manifest_sha256s=(first.content_hash,),
                    authoritative_master_snapshot_id="b" * 64,
                    authoritative_targets=targets,
                )
            self.assertEqual(replayed.content_hash, reference.content_hash)
            self.assertEqual(replayed.covered_target_count, 2)
            index = json.loads(Path(reference.object_path).read_text(encoding="utf-8"))
            self.assertFalse(index["ready"])
            self.assertFalse(index["complete"])
            rows = []
            for partition in index["partitions"]:
                content = Path(partition["object_path"]).read_text(encoding="utf-8")
                rows.extend(json.loads(line) for line in content.splitlines())
            by_code = {row["code"]: row for row in rows}
            self.assertEqual(by_code["600432.SH"]["industry_code"], "C32")
            self.assertEqual(by_code["000511.SZ"]["industry_code"], "C30")
            self.assertEqual(by_code["600432.SH"]["valid_from"], "2018-01-20")
            self.assertEqual(by_code["600432.SH"]["valid_to"], "2018-07-01")
            self.assertEqual(
                by_code["600432.SH"]["source_document_hash"],
                hashlib.sha256(self.pdf_2017).hexdigest(),
            )

    def test_real_pdf_parser_recovers_two_delisted_samples(self) -> None:
        fixture = Path("tmp/pdfs/industry_history_probe/2018Q1.pdf")
        if not fixture.is_file():
            self.skipTest("real CSRC PDF probe fixture is not retained")
        assignments, engine, _version, page_count, _text_hash = (
            source._extract_pdf_assignments(fixture.read_bytes())
        )
        by_code = {item.code: item.industry_code for item in assignments}
        self.assertEqual(engine, "pypdf")
        self.assertGreater(page_count, 80)
        self.assertGreater(len(assignments), 3_000)
        self.assertEqual(by_code["600432.SH"], "C32")
        self.assertEqual(by_code["000511.SZ"], "C30")

    def test_parser_handles_section_letter_wrapped_to_following_row(self) -> None:
        assignments = source._parse_assignment_texts(
            (
                "\n".join(
                    (
                        "门类名称及代码 行业大类代码 行业大类名称 上市公司代码 上市公司简称",
                        "农、林、牧、渔业 01 农业 000998 隆平高科",
                        "(A) 002041 登海种业",
                        "03 畜牧业 002477 雏鹰农牧",
                        "水利、环境和公共 77 生态保护和环境治理业 000005 世纪星源",
                        "设施管理业(N) 000035 中国天楹",
                    )
                ),
            )
        )
        by_code = {item.code: item.industry_code for item in assignments}
        self.assertEqual(by_code["000998.SZ"], "A01")
        self.assertEqual(by_code["002477.SZ"], "A03")
        self.assertEqual(by_code["000005.SZ"], "N77")
        self.assertEqual(by_code["000035.SZ"], "N77")


if __name__ == "__main__":
    unittest.main()
