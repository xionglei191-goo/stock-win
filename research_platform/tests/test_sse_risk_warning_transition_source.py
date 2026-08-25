from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from research_platform import sse_risk_warning_transition_source as transition


FIXED_NOW = datetime(2026, 8, 13, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _challenge_bytes(
    arg1: str = "0123456789ABCDEF0123456789ABCDEF01234567",
) -> bytes:
    positions = ",".join(hex(item) for item in transition._ACW_POSITION_LIST)
    return (
        "<html><script>"
        f"var arg1='{arg1}';"
        f"var posList=[{positions}];"
        "var mask=_0x1e8e('0x0');"
        f"'{transition._ACW_MASK_BASE64}';"
        '_0x4818("acw_sc__v2", arg1);document.location.reload()'
        "</script></html>"
    ).encode("utf-8")


def _index_bytes(spec: transition.SSERiskWarningTransitionSpec) -> bytes:
    entry = {
        "stock_code": spec.code,
        "SECURITY_NAME": spec.old_name,
        "bulletin_date": spec.publication_date,
        "bulletin_year": spec.publication_date[:4],
        "bulletin_large_type": "其它",
        "bulletin_small_type": "null",
        "bulletin_title": spec.announcement_title,
        "is_holder_disclose": "0",
        "bulletin_file_url": Path(spec.pdf_url).as_posix().replace(
            "https:/static.sse.com.cn", ""
        ),
        "bulletin_time": "null",
        "rtype": "",
        "xbrlFlag": "false",
        "bulletin_type": "2",
        "startDate": "",
        "endDate": "",
    }
    lines = [
        "//staticDate=2026-08-11 17:50:01",
        f"function get_{spec.code}(){{",
        "var _t = new Array();",
        "_t.push({",
    ]
    for index, (key, value) in enumerate(entry.items()):
        comma = "," if index < len(entry) - 1 else ""
        lines.append(
            f"   {key}:{json.dumps(value, ensure_ascii=False)}{comma}"
        )
    lines.extend(["});", "", "return _t;", "}"])
    return "\n".join(lines).encode("utf-8")


def _ascii_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font_reference}
            )
        }
    )
    stream = DecodedStreamObject()
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 10 Tf 40 740 Td ({escaped}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _fixture_spec() -> tuple[transition.SSERiskWarningTransitionSpec, bytes, bytes]:
    base = replace(
        transition.FROZEN_TRANSITION,
        legal_name="Wuhan Yifi Laser Corporation Limited",
        old_name="ST Yifi",
        new_name="Yifi Laser",
        announcement_title="Yifi Laser Risk Warning Removal Notice",
        announcement_number="2026-038",
        expected_index_sha256="0" * 64,
        expected_pdf_sha256="0" * 64,
    )
    index_raw = _index_bytes(base)
    pdf_text = " | ".join(
        [
            f"CODE:{base.code}",
            f"OLD:{base.old_name}",
            f"NUMBER:{base.announcement_number}",
            base.legal_name,
            "TITLE",
            f"NEW:{base.new_name}",
            "RISK:2025-05-06",
            "SUSPEND:2026-08-12",
            "EFFECTIVE:2026-08-13",
            f"RENAME:{base.old_name}->{base.new_name}",
            "RESUME:2026-08-13",
        ]
    )
    pdf_raw = _ascii_pdf(pdf_text)
    spec = replace(
        base,
        expected_index_sha256=hashlib.sha256(index_raw).hexdigest(),
        expected_pdf_sha256=hashlib.sha256(pdf_raw).hexdigest(),
    )
    return spec, index_raw, pdf_raw


class _Response:
    def __init__(
        self,
        content: bytes,
        url: str,
        content_type: str,
        *,
        status_code: int = 200,
        location: str | None = None,
    ) -> None:
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        if location is not None:
            self.headers["Location"] = location


class _Session:
    def __init__(
        self,
        spec: transition.SSERiskWarningTransitionSpec,
        index_raw: bytes,
        pdf_raw: bytes,
        *,
        redirect_source: str | None = None,
        status_code: int = 200,
        pdf_challenge: bytes | None = None,
        challenge_responses: int = 1,
    ) -> None:
        self.spec = spec
        self.index_raw = index_raw
        self.pdf_raw = pdf_raw
        self.redirect_source = redirect_source
        self.status_code = status_code
        self.pdf_challenge = pdf_challenge
        self.challenge_responses = challenge_responses
        self.pdf_calls = 0
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        if url == self.spec.index_url:
            raw = self.index_raw
            content_type = "application/javascript; charset=UTF-8"
            source_id = "index"
        elif url == self.spec.pdf_url:
            self.pdf_calls += 1
            if (
                self.pdf_challenge is not None
                and self.pdf_calls <= self.challenge_responses
            ):
                raw = self.pdf_challenge
                content_type = "text/html; charset=utf-8"
            else:
                raw = self.pdf_raw
                content_type = "application/pdf"
            source_id = "pdf"
        else:
            raise AssertionError(f"unexpected URL: {url}")
        response_url = (
            "https://evil.example/redirected"
            if self.redirect_source == source_id
            else url
        )
        return _Response(
            raw,
            response_url,
            content_type,
            status_code=self.status_code,
            location=response_url if self.redirect_source == source_id else None,
        )

    def post(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("transition collector must never POST")


class SSERiskWarningTransitionParserTests(unittest.TestCase):
    def test_fixed_acw_challenge_parser_is_exact_and_deterministic(self) -> None:
        raw = _challenge_bytes()
        first = transition._parse_acw_sc_v2_challenge(raw)
        second = transition._parse_acw_sc_v2_challenge(raw)
        self.assertEqual(first, second)
        self.assertRegex(first, r"\A[0-9a-f]{40}\Z")

        attacks = (
            raw.replace(transition._ACW_MASK_BASE64.encode("ascii"), b"changed"),
            raw.replace(b"document.location.reload()", b"history.back()"),
            raw.replace(b"var posList=[", b"var posList = ["),
            _challenge_bytes("0123456789abcdef0123456789abcdef01234567"),
        )
        for attacked in attacks:
            with self.subTest(attacked=attacked[:80]):
                with self.assertRaises(
                    transition.SSERiskWarningTransitionBlockedError
                ):
                    transition._parse_acw_sc_v2_challenge(attacked)

    def test_frozen_real_sources_reparse_when_local_cas_is_available(self) -> None:
        root = (
            Path(__file__).resolve().parents[2]
            / "data"
            / "security_master"
            / "sse_risk_warning_transition"
            / "cas"
        )
        cas = transition.SSERiskWarningTransitionCAS(root)
        paths = [
            cas.object_path(transition.FROZEN_TRANSITION.expected_index_sha256),
            cas.object_path(transition.FROZEN_TRANSITION.expected_pdf_sha256),
        ]
        if not all(path.is_file() for path in paths):
            self.skipTest("local immutable official SSE transition CAS is unavailable")
        parsed_index = transition.parse_fixed_index(
            cas.read_blob(transition.FROZEN_TRANSITION.expected_index_sha256)
        )
        parsed_pdf = transition.parse_fixed_notice_pdf(
            cas.read_blob(transition.FROZEN_TRANSITION.expected_pdf_sha256)
        )
        self.assertEqual(parsed_index["entry"]["stock_code"], "688646")
        self.assertEqual(parsed_pdf["effective_date"], "2026-08-13")

    def test_index_binds_code_old_name_title_date_and_pdf_url(self) -> None:
        spec, index_raw, _pdf_raw = _fixture_spec()
        parsed = transition.parse_fixed_index(index_raw, spec=spec)
        self.assertEqual(parsed["entry"]["stock_code"], spec.code)
        self.assertEqual(parsed["entry"]["SECURITY_NAME"], spec.old_name)
        self.assertEqual(parsed["entry"]["bulletin_title"], spec.announcement_title)

        for field, bad in (
            ("stock_code", "688647"),
            ("SECURITY_NAME", "Wrong Name"),
            ("bulletin_date", "2026-08-11"),
            ("bulletin_title", "Wrong title"),
            ("bulletin_file_url", "/wrong.pdf"),
        ):
            with self.subTest(field=field):
                changed = index_raw.replace(
                    json.dumps(
                        parsed["entry"][field], ensure_ascii=False
                    ).encode("utf-8"),
                    json.dumps(bad, ensure_ascii=False).encode("utf-8"),
                    1,
                )
                changed_spec = replace(
                    spec, expected_index_sha256=hashlib.sha256(changed).hexdigest()
                )
                with self.assertRaises(transition.SSERiskWarningTransitionBlockedError):
                    transition.parse_fixed_index(changed, spec=changed_spec)

    def test_pdf_reparse_binds_all_transition_fields(self) -> None:
        spec, _index_raw, pdf_raw = _fixture_spec()
        with patch.object(
            transition,
            "_extract_pdf_text",
            return_value=(
                "fixture PDF text",
                "pypdf",
                "TEST",
                1,
            ),
        ), patch.object(
            transition, "_compact_text", return_value="FIXTURE_COMPACT"
        ), patch.object(
            transition,
            "_notice_markers",
            return_value={"all": "FIXTURE_COMPACT"},
        ):
            parsed = transition.parse_fixed_notice_pdf(pdf_raw, spec=spec)
        self.assertEqual(parsed["code_alias"], "688646.SH")
        self.assertEqual(parsed["old_name"], "ST Yifi")
        self.assertEqual(parsed["new_name"], "Yifi Laser")
        self.assertEqual(parsed["risk_started_at"], "2025-05-06")
        self.assertEqual(parsed["suspension_date"], "2026-08-12")
        self.assertEqual(parsed["effective_date"], "2026-08-13")
        self.assertEqual(parsed["extraction_engine"], "pypdf")

    def test_real_pypdf_extraction_is_recomputed_from_raw_pdf(self) -> None:
        spec, _index_raw, pdf_raw = _fixture_spec()
        text, engine, version, pages = transition._extract_pdf_text(pdf_raw)
        self.assertIn("CODE:688646", text)
        self.assertEqual(engine, "pypdf")
        self.assertTrue(version)
        self.assertEqual(pages, 1)

    def test_pdf_code_name_and_dates_fail_closed(self) -> None:
        spec, _index_raw, pdf_raw = _fixture_spec()
        admitted_markers = transition._notice_markers(spec)
        fixture_text = "|".join(admitted_markers.values())
        for kwargs in (
            {"code": "688647"},
            {"old_name": "ST Wrong"},
            {"new_name": "Wrong New"},
            {"risk_started_at": "2025-05-07"},
            {"suspension_date": "2026-08-11", "publication_date": "2026-08-11"},
            {"effective_date": "2026-08-14"},
        ):
            with self.subTest(kwargs=kwargs):
                changed = replace(spec, **kwargs)
                conflict = transition._notice_markers(changed)
                with self.assertRaises(transition.SSERiskWarningTransitionBlockedError):
                    with patch.object(
                        transition,
                        "_extract_pdf_text",
                        return_value=(fixture_text, "pypdf", "TEST", 1),
                    ), patch.object(
                        transition,
                        "_compact_text",
                        side_effect=lambda value: str(value).replace(" ", ""),
                    ), patch.object(
                        transition,
                        "_notice_markers",
                        return_value=conflict,
                    ):
                        transition.parse_fixed_notice_pdf(pdf_raw, spec=changed)

    @staticmethod
    def _parse_ascii_fixture(
        pdf_raw: bytes, spec: transition.SSERiskWarningTransitionSpec
    ) -> dict[str, object]:
        with patch.object(
            transition,
            "_extract_pdf_text",
            return_value=("fixture PDF text", "pypdf", "TEST", 1),
        ), patch.object(
            transition, "_compact_text", return_value="FIXTURE_COMPACT"
        ), patch.object(
            transition,
            "_notice_markers",
            return_value={"all": "FIXTURE_COMPACT"},
        ):
            return transition.parse_fixed_notice_pdf(pdf_raw, spec=spec)


class SSERiskWarningTransitionEndToEndTests(unittest.TestCase):
    def _build(
        self, directory: str, **session_kwargs: object
    ) -> tuple[
        transition.SSERiskWarningTransitionArtifact,
        transition.SSERiskWarningTransitionCAS,
        _Session,
        transition.SSERiskWarningTransitionSpec,
    ]:
        spec, index_raw, pdf_raw = _fixture_spec()
        session = _Session(spec, index_raw, pdf_raw, **session_kwargs)
        cas = transition.SSERiskWarningTransitionCAS(Path(directory))
        fixture_markers = transition._notice_markers(spec)
        fixture_text = "|".join(fixture_markers.values())
        with patch.object(transition, "FROZEN_TRANSITION", spec), patch.object(
            transition,
            "_extract_pdf_text",
            return_value=(fixture_text, "pypdf", "TEST", 1),
        ):
            artifact = transition.SSERiskWarningTransitionClient(
                cas=cas,
                session=session,
                clock=lambda: FIXED_NOW,
            ).fetch()
        return artifact, cas, session, spec

    def test_get_only_cas_cold_replay_and_audit_only_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, session, spec = self._build(directory)
            self.assertEqual([call["method"] for call in session.calls], ["GET", "GET"])
            self.assertTrue(
                all(call["allow_redirects"] is False for call in session.calls)
            )
            self.assertEqual(artifact.transition.code_alias, "688646.SH")
            self.assertFalse(artifact.source_contract["ready"])
            self.assertFalse(artifact.source_contract["caller_summary_trusted"])
            self.assertFalse(artifact.source_contract["training_allowed"])
            self.assertFalse(artifact.source_contract["trading_allowed"])
            self.assertFalse(
                artifact.source_contract["historical_master_integration_allowed"]
            )
            with patch.object(transition, "FROZEN_TRANSITION", spec):
                with patch.object(
                    transition,
                    "_extract_pdf_text",
                    return_value=(
                        "|".join(transition._notice_markers(spec).values()),
                        "pypdf",
                        "TEST",
                        1,
                    ),
                ):
                    store = transition.SSERiskWarningTransitionManifestStore(cas)
                    reference = store.seal(artifact)
                    cold = transition.SSERiskWarningTransitionManifestStore(
                        transition.SSERiskWarningTransitionCAS(Path(directory))
                    ).replay(reference.manifest_sha256)
            self.assertEqual(cold.logical_content_sha256, artifact.logical_content_sha256)
            self.assertFalse(reference.ready)

    def test_pdf_challenge_allows_one_same_url_get_retry_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            challenge = _challenge_bytes()
            artifact, _cas, session, _spec = self._build(
                directory,
                pdf_challenge=challenge,
            )
            self.assertEqual(
                [call["method"] for call in session.calls],
                ["GET", "GET", "GET"],
            )
            self.assertTrue(
                all(call["allow_redirects"] is False for call in session.calls)
            )
            self.assertNotIn("Cookie", session.calls[1]["headers"])
            expected_cookie = transition._parse_acw_sc_v2_challenge(challenge)
            self.assertEqual(
                session.calls[2]["headers"]["Cookie"],
                f"acw_sc__v2={expected_cookie}",
            )
            self.assertEqual(
                artifact.raw_evidence[1].body.sha256,
                artifact.transition.pdf_sha256,
            )

    def test_invalid_or_repeated_pdf_challenge_fails_closed(self) -> None:
        invalid = _challenge_bytes().replace(
            transition._ACW_MASK_BASE64.encode("ascii"),
            b"changed",
        )
        for label, challenge, repeat_count in (
            ("invalid", invalid, 1),
            ("repeated", _challenge_bytes(), 2),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(
                    transition.SSERiskWarningTransitionBlockedError
                ):
                    self._build(
                        directory,
                        pdf_challenge=challenge,
                        challenge_responses=repeat_count,
                    )

    def test_redirects_and_hash_mismatches_fail_closed(self) -> None:
        for source in ("index", "pdf"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(transition.SSERiskWarningTransitionBlockedError):
                    self._build(directory, redirect_source=source)

        spec, index_raw, pdf_raw = _fixture_spec()
        for label, changed_index, changed_pdf in (
            ("index", index_raw + b"\n", pdf_raw),
            ("pdf", index_raw, pdf_raw + b"\n"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                session = _Session(spec, changed_index, changed_pdf)
                with patch.object(transition, "FROZEN_TRANSITION", spec):
                    with self.assertRaisesRegex(
                        transition.SSERiskWarningTransitionBlockedError, "SHA-256 mismatch"
                    ):
                        transition.SSERiskWarningTransitionClient(
                            cas=transition.SSERiskWarningTransitionCAS(Path(directory)),
                            session=session,
                            clock=lambda: FIXED_NOW,
                        ).fetch()

    def test_caller_summary_and_ready_cannot_self_attest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session, spec = self._build(directory)
            attacks = (
                replace(
                    artifact,
                    transition=replace(artifact.transition, new_name="Forged"),
                ),
                replace(artifact, logical_content_sha256="0" * 64),
            )
            with patch.object(transition, "FROZEN_TRANSITION", spec):
                with patch.object(
                    transition,
                    "_extract_pdf_text",
                    return_value=(
                        "|".join(transition._notice_markers(spec).values()),
                        "pypdf",
                        "TEST",
                        1,
                    ),
                ):
                    store = transition.SSERiskWarningTransitionManifestStore(cas)
                    for attacked in attacks:
                        with self.subTest(attacked=attacked):
                            with self.assertRaises(transition.SSERiskWarningTransitionBlockedError):
                                store.seal(attacked)

                    payload = transition._manifest_payload(artifact)
                    payload["source_contract"]["ready"] = True
                    manifest = transition._canonical_json_bytes(payload)
                    reference = cas.put_blob(manifest)
                    with self.assertRaisesRegex(
                        transition.SSERiskWarningTransitionBlockedError,
                        "source contract drift",
                    ):
                        store.replay(reference.sha256)

                    payload = transition._manifest_payload(artifact)
                    payload["statistics"]["ready_transition_count"] = 1
                    manifest = transition._canonical_json_bytes(payload)
                    reference = cas.put_blob(manifest)
                    with self.assertRaisesRegex(
                        transition.SSERiskWarningTransitionBlockedError,
                        "statistics drift",
                    ):
                        store.replay(reference.sha256)

    def test_manifest_rejects_replaced_or_duplicated_raw_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session, spec = self._build(directory)
            with patch.object(transition, "FROZEN_TRANSITION", spec), patch.object(
                transition,
                "_extract_pdf_text",
                return_value=(
                    "|".join(transition._notice_markers(spec).values()),
                    "pypdf",
                    "TEST",
                    1,
                ),
            ):
                store = transition.SSERiskWarningTransitionManifestStore(cas)
                for attacked in (
                    replace(
                        artifact,
                        raw_evidence=(
                            artifact.raw_evidence[0],
                            artifact.raw_evidence[0],
                        ),
                    ),
                    replace(
                        artifact,
                        raw_evidence=(
                            artifact.raw_evidence[1],
                            artifact.raw_evidence[0],
                        ),
                    ),
                ):
                    with self.subTest(attacked=attacked):
                        with self.assertRaises(
                            transition.SSERiskWarningTransitionBlockedError
                        ):
                            store.seal(attacked)

    def test_raw_cas_tamper_reparse_and_toctou_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session, spec = self._build(directory)
            with patch.object(transition, "FROZEN_TRANSITION", spec):
                with patch.object(
                    transition,
                    "_extract_pdf_text",
                    return_value=(
                        "|".join(transition._notice_markers(spec).values()),
                        "pypdf",
                        "TEST",
                        1,
                    ),
                ):
                    store = transition.SSERiskWarningTransitionManifestStore(cas)
                    reference = store.seal(artifact)
                    target = cas.object_path(artifact.raw_evidence[0].body.sha256)
                    target.write_bytes(b"tampered")
                    with self.assertRaisesRegex(
                        transition.SSERiskWarningTransitionBlockedError,
                        "hash or size",
                    ):
                        store.replay(reference.manifest_sha256)

        with tempfile.TemporaryDirectory() as directory:
            cas = transition.SSERiskWarningTransitionCAS(Path(directory))
            with patch.object(transition, "_path_is_link_or_reparse", return_value=True):
                with self.assertRaisesRegex(
                    transition.SSERiskWarningTransitionBlockedError, "reparse"
                ):
                    cas.put_blob(b"blocked")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value"
            path.write_bytes(b"before")
            original_fstat = transition.os.fstat
            calls = 0

            def drifting_fstat(descriptor: int):
                nonlocal calls
                value = original_fstat(descriptor)
                calls += 1
                if calls >= 2:
                    return SimpleNamespace(
                        st_dev=value.st_dev,
                        st_ino=value.st_ino,
                        st_size=value.st_size,
                        st_mtime_ns=value.st_mtime_ns + 1,
                        st_mode=value.st_mode,
                        st_file_attributes=getattr(value, "st_file_attributes", 0),
                    )
                return value

            with patch.object(transition.os, "fstat", drifting_fstat):
                with self.assertRaisesRegex(
                    transition.SSERiskWarningTransitionBlockedError,
                    "changed during read",
                ):
                    transition._stable_read(Path(directory), path, "TOCTOU fixture")


if __name__ == "__main__":
    unittest.main()
