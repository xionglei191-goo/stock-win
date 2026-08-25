from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import research_platform.szse_code_change_events as events
from research_platform.szse_code_change_events import (
    EFFECTIVE_DATE,
    NEW_CODE,
    OLD_CODE,
    PRIMARY_DISCLOSURE_URL,
    SOURCE_CONTRACT_ADMITTED,
    SOURCE_CONTRACT_UNADMITTED,
    SZSECodeChangeBlockedError,
    SZSECodeChangeClient,
    SZSEDisclosureCAS,
    parse_szse_code_change_pdf,
    validate_alias_intervals,
)


RETRIEVED_AT = "2026-08-13T09:30:00+08:00"
EVENT_TEXT = (
    "本公司证券简称由中航电测变更为中航成飞，"
    "证券代码由300114变更为302132。"
    "上述证券简称和证券代码变更自2025年2月17日起生效，"
    "属于同一上市公司证券身份的连续变更。"
)


def _pdf(seed: bytes = b"fixture") -> bytes:
    return b"%PDF-1.7\n" + seed + b"\n%%EOF\n"


class _Response:
    def __init__(
        self,
        content: bytes,
        *,
        url: str = PRIMARY_DISCLOSURE_URL,
        status_code: int = 200,
        content_type: str = "application/pdf",
        history: list[Any] | None = None,
    ) -> None:
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.history = [] if history is None else history


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.response


def _extracted(text: str = EVENT_TEXT) -> events._ExtractedText:
    return events._ExtractedText(
        text=text,
        engine="pypdf",
        engine_version="TEST",
        page_count=3,
    )


class SZSECodeChangeEventTests(unittest.TestCase):
    def _capture(
        self, directory: str, raw: bytes, *, source_url: str = PRIMARY_DISCLOSURE_URL
    ) -> events.RawPDFEvidence:
        return SZSEDisclosureCAS(Path(directory) / "raw").capture(
            raw,
            source_url=source_url,
            retrieved_at=RETRIEVED_AT,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def test_recomputed_primary_pdf_creates_one_atomic_entity_chain(self) -> None:
        raw = _pdf()
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._capture(directory, raw)
            with patch.object(events, "_extract_text_from_pdf", return_value=_extracted()):
                artifact = parse_szse_code_change_pdf(
                    raw,
                    raw_evidence=evidence,
                )

        self.assertTrue(artifact.ready)
        self.assertEqual(artifact.status, SOURCE_CONTRACT_ADMITTED)
        self.assertEqual([item.code_alias for item in artifact.intervals], [OLD_CODE, NEW_CODE])
        old, new = artifact.intervals
        self.assertEqual(old.canonical_entity_id, new.canonical_entity_id)
        self.assertIsNone(old.valid_from)
        self.assertEqual(old.valid_to, EFFECTIVE_DATE)
        self.assertEqual(new.valid_from, EFFECTIVE_DATE)
        self.assertIsNone(new.valid_to)
        self.assertEqual(old.source_hash, hashlib.sha256(raw).hexdigest())
        self.assertEqual(old.source_url, PRIMARY_DISCLOSURE_URL)
        self.assertEqual(old.retrieved_at, RETRIEVED_AT)
        self.assertTrue(artifact.text_evidence.recomputed_from_raw)  # type: ignore[union-attr]
        self.assertEqual(artifact.text_evidence.raw_pdf_sha256, old.source_hash)  # type: ignore[union-attr]

    def test_cas_and_expected_hash_are_tamper_evident(self) -> None:
        raw = _pdf(b"original")
        with tempfile.TemporaryDirectory() as directory:
            cas = SZSEDisclosureCAS(Path(directory) / "raw")
            with self.assertRaisesRegex(SZSECodeChangeBlockedError, "hash mismatch"):
                cas.capture(
                    _pdf(b"tampered"),
                    source_url=PRIMARY_DISCLOSURE_URL,
                    retrieved_at=RETRIEVED_AT,
                    expected_sha256=hashlib.sha256(raw).hexdigest(),
                )

            evidence = cas.capture(
                raw,
                source_url=PRIMARY_DISCLOSURE_URL,
                retrieved_at=RETRIEVED_AT,
            )
            Path(evidence.object_path).write_bytes(_pdf(b"changed-after-capture"))
            with self.assertRaisesRegex(SZSECodeChangeBlockedError, "tampered"):
                parse_szse_code_change_pdf(raw, raw_evidence=evidence)

    def test_wrong_code_name_or_effective_date_fails_closed(self) -> None:
        invalid_texts = {
            "old code": EVENT_TEXT.replace("300114", "300115"),
            "new code": EVENT_TEXT.replace("302132", "302133"),
            "old name": EVENT_TEXT.replace("中航电测", "另一公司"),
            "new name": EVENT_TEXT.replace("中航成飞", "另一公司"),
            "date": EVENT_TEXT.replace("2025年2月17日", "2025年2月18日"),
        }
        raw = _pdf()
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._capture(directory, raw)
            for label, text in invalid_texts.items():
                with self.subTest(label=label):
                    with patch.object(
                        events, "_extract_text_from_pdf", return_value=_extracted(text)
                    ):
                        with self.assertRaises(SZSECodeChangeBlockedError):
                            parse_szse_code_change_pdf(raw, raw_evidence=evidence)

    def test_overlap_or_non_atomic_boundary_is_rejected(self) -> None:
        raw = _pdf()
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._capture(directory, raw)
            with patch.object(events, "_extract_text_from_pdf", return_value=_extracted()):
                intervals = parse_szse_code_change_pdf(
                    raw, raw_evidence=evidence
                ).intervals

        overlapping = (intervals[0], replace(intervals[1], valid_from="2025-02-16"))
        with self.assertRaises(SZSECodeChangeBlockedError):
            validate_alias_intervals(overlapping)
        gapped = (replace(intervals[0], valid_to="2025-02-16"), intervals[1])
        with self.assertRaises(SZSECodeChangeBlockedError):
            validate_alias_intervals(gapped)

    def test_wrong_host_and_unfrozen_path_are_rejected_before_cas_write(self) -> None:
        raw = _pdf()
        invalid_urls = (
            PRIMARY_DISCLOSURE_URL.replace("disc.static.szse.cn", "example.com"),
            "https://disc.static.szse.cn/disc/unfrozen.PDF",
            PRIMARY_DISCLOSURE_URL + "?download=1",
        )
        with tempfile.TemporaryDirectory() as directory:
            cas = SZSEDisclosureCAS(Path(directory) / "raw")
            for source_url in invalid_urls:
                with self.subTest(source_url=source_url):
                    with self.assertRaises(SZSECodeChangeBlockedError):
                        cas.capture(
                            raw,
                            source_url=source_url,
                            retrieved_at=RETRIEVED_AT,
                        )

    def test_hash_linked_supplied_text_never_self_admits(self) -> None:
        raw = _pdf()
        raw_hash = hashlib.sha256(raw).hexdigest()
        text_hash = hashlib.sha256(EVENT_TEXT.encode("utf-8")).hexdigest()
        extraction_failure = SZSECodeChangeBlockedError(
            "no reproducible text layer", status=SOURCE_CONTRACT_UNADMITTED
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._capture(directory, raw)
            with patch.object(
                events, "_extract_text_from_pdf", side_effect=extraction_failure
            ):
                artifact = parse_szse_code_change_pdf(
                    raw,
                    raw_evidence=evidence,
                    extracted_text=EVENT_TEXT,
                    extracted_text_sha256=text_hash,
                    extracted_from_raw_sha256=raw_hash,
                )

        self.assertFalse(artifact.ready)
        self.assertEqual(artifact.status, SOURCE_CONTRACT_UNADMITTED)
        self.assertEqual(len(artifact.intervals), 2)
        self.assertFalse(artifact.text_evidence.recomputed_from_raw)  # type: ignore[union-attr]
        self.assertFalse(artifact.to_dict()["promotion_allowed"])

    def test_unextractable_raw_without_text_stays_unadmitted_and_empty(self) -> None:
        raw = _pdf()
        extraction_failure = SZSECodeChangeBlockedError(
            "no reproducible text layer", status=SOURCE_CONTRACT_UNADMITTED
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._capture(directory, raw)
            with patch.object(
                events, "_extract_text_from_pdf", side_effect=extraction_failure
            ):
                artifact = parse_szse_code_change_pdf(raw, raw_evidence=evidence)

        self.assertFalse(artifact.ready)
        self.assertEqual(artifact.status, SOURCE_CONTRACT_UNADMITTED)
        self.assertEqual(artifact.intervals, ())
        self.assertIsNone(artifact.text_evidence)

    def test_supplied_text_requires_both_raw_and_text_hashes(self) -> None:
        raw = _pdf()
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._capture(directory, raw)
            for raw_hash, text_hash in (
                ("0" * 64, hashlib.sha256(EVENT_TEXT.encode("utf-8")).hexdigest()),
                (hashlib.sha256(raw).hexdigest(), "0" * 64),
            ):
                with self.subTest(raw_hash=raw_hash, text_hash=text_hash):
                    with self.assertRaises(SZSECodeChangeBlockedError):
                        parse_szse_code_change_pdf(
                            raw,
                            raw_evidence=evidence,
                            extracted_text=EVENT_TEXT,
                            extracted_text_sha256=text_hash,
                            extracted_from_raw_sha256=raw_hash,
                        )

    def test_supplied_text_cannot_override_recomputed_pdf_text(self) -> None:
        raw = _pdf()
        supplied = EVENT_TEXT + "附加调用者文字"
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._capture(directory, raw)
            with patch.object(events, "_extract_text_from_pdf", return_value=_extracted()):
                with self.assertRaisesRegex(SZSECodeChangeBlockedError, "differs"):
                    parse_szse_code_change_pdf(
                        raw,
                        raw_evidence=evidence,
                        extracted_text=supplied,
                        extracted_text_sha256=hashlib.sha256(
                            supplied.encode("utf-8")
                        ).hexdigest(),
                        extracted_from_raw_sha256=hashlib.sha256(raw).hexdigest(),
                    )

    def test_client_is_get_only_rejects_redirects_and_preserves_raw_cas(self) -> None:
        raw = _pdf()
        session = _Session(_Response(raw))
        with tempfile.TemporaryDirectory() as directory:
            client = SZSECodeChangeClient(
                session=session,  # type: ignore[arg-type]
                cas=SZSEDisclosureCAS(Path(directory) / "raw"),
            )
            with patch.object(events, "_extract_text_from_pdf", return_value=_extracted()):
                artifact = client.fetch_primary(
                    retrieved_at=RETRIEVED_AT,
                    expected_sha256=hashlib.sha256(raw).hexdigest(),
                )
            self.assertTrue(Path(artifact.raw_evidence.object_path).is_file())

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["url"], PRIMARY_DISCLOSURE_URL)
        self.assertFalse(session.calls[0]["allow_redirects"])

        redirected = _Session(
            _Response(
                raw,
                url=PRIMARY_DISCLOSURE_URL.replace("/download/", "/"),
                history=[object()],
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            client = SZSECodeChangeClient(
                session=redirected,  # type: ignore[arg-type]
                cas=SZSEDisclosureCAS(Path(directory) / "raw"),
            )
            with self.assertRaisesRegex(SZSECodeChangeBlockedError, "redirected"):
                client.fetch_primary(retrieved_at=RETRIEVED_AT)


if __name__ == "__main__":
    unittest.main()
