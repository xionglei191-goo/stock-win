from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from research_platform import bse_current_delisting_events as current


FIXED_NOW = datetime(2026, 8, 13, 1, 0, tzinfo=timezone(timedelta(hours=8)))


class _Response:
    def __init__(
        self,
        url: str,
        content: bytes,
        content_type: str | None,
        *,
        status_code: int = 200,
        response_url: str | None = None,
        location: str | None = None,
        set_cookie: str | None = None,
        history: tuple[object, ...] = (),
    ) -> None:
        self.url = response_url or url
        self.content = content
        self.status_code = status_code
        self.history = history
        self.headers: dict[str, str] = {}
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        if location is not None:
            self.headers["Location"] = location
        if set_cookie is not None:
            self.headers["Set-Cookie"] = set_cookie


def _fake_pdf_fixture() -> tuple[
    tuple[current.BSEDelistingNoticeSpec, ...],
    dict[str, bytes],
]:
    raw_by_url: dict[str, bytes] = {}
    specs: list[current.BSEDelistingNoticeSpec] = []
    for spec in current.NOTICE_SPECS:
        raw = f"%PDF-1.7\nfixture-{spec.code}\n%%EOF".encode("ascii")
        raw_by_url[spec.source_url] = raw
        specs.append(replace(spec, expected_sha256=hashlib.sha256(raw).hexdigest()))
    return tuple(specs), raw_by_url


def _fake_pdf_extract(raw: bytes) -> tuple[str, str, str, int]:
    code = "920305" if b"920305" in raw else "920680"
    spec = next(item for item in current.NOTICE_SPECS if item.code == code)
    effective = datetime.strptime(spec.effective_date, "%Y-%m-%d").date()
    text = (
        f"证券代码：{spec.code} 证券简称：测试退 "
        f"公告编号：{spec.announcement_number} "
        f"{spec.legal_name}关于公司股票终止上市暨摘牌的公告 "
        f"公司股票将于{effective.year}年{effective.month}月{effective.day}日"
        "被北京证券交易所终止上市并摘牌。"
    )
    return text, "fixture", "1", 3


def _catalogue_row(code: str) -> dict[str, object]:
    row: dict[str, object] = {field: "" for field in current.CATALOGUE_ROW_FIELDS}
    row.update(
        {
            "xxzqdm": code,
            "xxzqjc": f"测试{code}",
            "xxzqjb": "T",
            "xxfcbj": "2",
            "xxjsrq": "20260812",
        }
    )
    return row


def _catalogue_payload(
    page: int,
    *,
    codes: tuple[str, ...] = ("920001", "920002", "920003"),
    duplicate: bool = False,
) -> bytes:
    page_size = 2
    total = len(codes)
    total_pages = (total + page_size - 1) // page_size
    selected = list(codes[page * page_size : (page + 1) * page_size])
    if duplicate and len(selected) > 1:
        selected[-1] = selected[0]
    value = [
        {
            "content": [_catalogue_row(code) for code in selected],
            "firstPage": page == 0,
            "lastPage": page == total_pages - 1,
            "number": page,
            "numberOfElements": len(selected),
            "size": page_size,
            "sort": None,
            "totalElements": total,
            "totalPages": total_pages,
        }
    ]
    return b"null(" + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    ) + b")"


class _Session:
    def __init__(
        self,
        *,
        specs: tuple[current.BSEDelistingNoticeSpec, ...],
        raw_by_url: dict[str, bytes],
        challenge_first: bool = True,
        challenge_location: str | None = None,
        second_is_redirect: bool = False,
        catalogue_response_url: str | None = None,
        catalogue_codes: tuple[str, ...] = ("920001", "920002", "920003"),
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.catalogue_response_url = catalogue_response_url
        self.catalogue_codes = catalogue_codes
        self._get_queues: dict[str, list[_Response]] = {}
        for index, spec in enumerate(specs):
            raw = raw_by_url[spec.source_url]
            queue: list[_Response] = []
            if challenge_first and index == 0:
                queue.append(
                    _Response(
                        spec.source_url,
                        b"challenge",
                        "text/html;charset=UTF-8",
                        status_code=302,
                        location=(
                            challenge_location
                            if challenge_location is not None
                            else spec.source_url
                        ),
                        set_cookie="C3VK=fixture-cookie; Path=/; Secure",
                    )
                )
                if second_is_redirect:
                    queue.append(
                        _Response(
                            spec.source_url,
                            b"challenge-again",
                            "text/html",
                            status_code=302,
                            location=spec.source_url,
                            set_cookie="C3VK=second-cookie; Path=/; Secure",
                        )
                    )
                else:
                    queue.append(
                        _Response(spec.source_url, raw, "application/pdf;charset=binary")
                    )
            else:
                queue.append(_Response(spec.source_url, raw, "application/pdf"))
            self._get_queues[spec.source_url] = queue

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._get_queues[url].pop(0)

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        form = dict(kwargs["data"])  # type: ignore[arg-type]
        page = int(form["page"])
        return _Response(
            url,
            _catalogue_payload(page, codes=self.catalogue_codes),
            "text/html;charset=utf-8",
            response_url=self.catalogue_response_url,
        )


class BSECurrentDelistingParserTests(unittest.TestCase):
    def test_real_official_notice_fixtures_reparse_when_local_cas_is_available(
        self,
    ) -> None:
        root = (
            Path(__file__).resolve().parents[2]
            / "data"
            / "security_master"
            / "bse_current_delisting"
            / "cas"
        )
        cas = current.BSECurrentDelistingCAS(root)
        for spec in current.NOTICE_SPECS:
            path = cas._blob_path(spec.expected_sha256)
            if not path.is_file():
                self.skipTest("local official BSE notice CAS fixture is unavailable")
            raw = cas.read_blob(
                current.BlobReference(
                    spec.expected_sha256,
                    path.stat().st_size,
                    f"sha256:{spec.expected_sha256}",
                )
            )
            parsed = current.parse_notice_pdf(raw, spec=spec)
            self.assertEqual(parsed["code_alias"], spec.code_alias)
            self.assertEqual(parsed["effective_date"], spec.effective_date)

    def test_pdf_reparse_binds_code_legal_name_and_effective_date(self) -> None:
        specs, raw_by_url = _fake_pdf_fixture()
        with (
            patch.object(current, "NOTICE_SPECS", specs),
            patch.object(current, "_extract_pdf_text", side_effect=_fake_pdf_extract),
        ):
            parsed = current.parse_notice_pdf(
                raw_by_url[specs[0].source_url], spec=specs[0]
            )
            self.assertEqual(parsed["code_alias"], "920305.BJ")
            self.assertEqual(parsed["effective_date"], "2026-07-30")
            self.assertEqual(parsed["event_type"], "TERMINATED_LISTING")

            unrelated = "无关公司和无关日期"
            with patch.object(
                current,
                "_extract_pdf_text",
                return_value=(unrelated, "fixture", "1", 1),
            ):
                with self.assertRaisesRegex(
                    current.BSECurrentDelistingBlockedError, "lacks identity"
                ):
                    current.parse_notice_pdf(
                        raw_by_url[specs[0].source_url], spec=specs[0]
                    )

    def test_catalogue_parser_rejects_duplicate_schema_and_target_presence(self) -> None:
        with patch.object(current, "BSE_CATALOGUE_PAGE_SIZE", 2):
            parsed = current.parse_current_catalogue_page(
                _catalogue_payload(0), request_page=0, minimum_rows=3
            )
            self.assertEqual(parsed.codes, ("920001", "920002"))
            with self.assertRaisesRegex(
                current.BSECurrentDelistingBlockedError, "duplicate or unsorted"
            ):
                current.parse_current_catalogue_page(
                    _catalogue_payload(0, duplicate=True),
                    request_page=0,
                    minimum_rows=3,
                )

            value = json.loads(_catalogue_payload(0)[5:-1])
            del value[0]["content"][0]["xxisin"]
            changed = b"null(" + json.dumps(
                value, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8") + b")"
            with self.assertRaisesRegex(
                current.BSECurrentDelistingBlockedError, "row schema drift"
            ):
                current.parse_current_catalogue_page(
                    changed, request_page=0, minimum_rows=3
                )


class BSECurrentDelistingEndToEndTests(unittest.TestCase):
    def test_local_real_manifest_cold_replays_when_cas_is_available(self) -> None:
        root = (
            Path(__file__).resolve().parents[2]
            / "data"
            / "security_master"
            / "bse_current_delisting"
            / "cas"
        )
        manifest_sha256 = (
            "9a405d4e2499615abaca659fe08ede6f101cef2c1bbdb3a73623488664cbd8dd"
        )
        manifest_path = root / "manifests" / f"{manifest_sha256}.json"
        if not manifest_path.is_file():
            self.skipTest("local real BSE current-delisting manifest is unavailable")
        artifact = current.BSECurrentDelistingManifestStore(
            current.BSECurrentDelistingCAS(root)
        ).replay(manifest_sha256)
        self.assertEqual(
            artifact.logical_content_sha256,
            "2f713be4941af59b2038728b4c0d6df9dd199840514f665fa10ca1bfe6edf728",
        )
        self.assertEqual(
            [item.code_alias for item in artifact.events],
            ["920305.BJ", "920680.BJ"],
        )
        self.assertEqual(artifact.completeness["catalogue_total_elements"], 335)

    def _build(
        self,
        directory: str,
        **session_kwargs: object,
    ) -> tuple[
        current.BSECurrentDelistingArtifact,
        current.BSECurrentDelistingCAS,
        _Session,
        tuple[current.BSEDelistingNoticeSpec, ...],
    ]:
        specs, raw_by_url = _fake_pdf_fixture()
        session = _Session(specs=specs, raw_by_url=raw_by_url, **session_kwargs)
        cas = current.BSECurrentDelistingCAS(Path(directory))
        with (
            patch.object(current, "NOTICE_SPECS", specs),
            patch.object(current, "BSE_CATALOGUE_PAGE_SIZE", 2),
            patch.object(current, "BSE_CATALOGUE_MINIMUM_ROWS", 3),
            patch.object(current, "_extract_pdf_text", side_effect=_fake_pdf_extract),
        ):
            artifact = current.BSECurrentDelistingClient(
                cas=cas,
                session=session,
                clock=lambda: FIXED_NOW,
            ).fetch_current()
        return artifact, cas, session, specs

    def test_challenge_post_pagination_cas_cold_replay_and_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, session, specs = self._build(directory)
            self.assertEqual(
                [item.code_alias for item in artifact.events],
                ["920305.BJ", "920680.BJ"],
            )
            self.assertEqual(
                [item.effective_date for item in artifact.events],
                ["2026-07-30", "2026-01-05"],
            )
            self.assertEqual(len(artifact.notices[0].transport_attempts), 2)
            self.assertEqual(len(artifact.notices[1].transport_attempts), 1)
            self.assertTrue(artifact.completeness["full_pagination_closed"])
            self.assertTrue(
                artifact.source_contract["current_catalogue_is_reconciliation_only"]
            )
            self.assertFalse(
                artifact.source_contract["current_catalogue_contributes_historical_dates"]
            )
            self.assertTrue(
                all(call["allow_redirects"] is False for call in session.calls)
            )
            self.assertEqual(
                {call["method"] for call in session.calls}, {"GET", "POST"}
            )
            retry = [call for call in session.calls if call["method"] == "GET"][1]
            self.assertTrue(
                str(retry["headers"]["Cookie"]).startswith("C3VK=")  # type: ignore[index]
            )
            post_calls = [call for call in session.calls if call["method"] == "POST"]
            self.assertEqual(len(post_calls), 3)
            self.assertEqual(
                post_calls[0]["data"], list(current._catalogue_form_fields(0))
            )
            self.assertEqual(
                post_calls[1]["data"], list(current._catalogue_form_fields(1))
            )
            self.assertEqual(
                post_calls[2]["data"], list(current._catalogue_form_fields(0))
            )
            self.assertEqual(
                cas.read_blob(artifact.notices[0].transport_attempts[0].body),
                b"challenge",
            )

            with (
                patch.object(current, "NOTICE_SPECS", specs),
                patch.object(current, "BSE_CATALOGUE_PAGE_SIZE", 2),
                patch.object(current, "BSE_CATALOGUE_MINIMUM_ROWS", 3),
                patch.object(current, "_extract_pdf_text", side_effect=_fake_pdf_extract),
            ):
                reference = current.BSECurrentDelistingManifestStore(cas).seal(artifact)
                cold = current.BSECurrentDelistingManifestStore(
                    current.BSECurrentDelistingCAS(Path(directory))
                ).replay(reference.manifest_sha256)
                self.assertEqual(
                    cold.logical_content_sha256, artifact.logical_content_sha256
                )
                current.validate_current_delisting_freshness(
                    cold,
                    now=FIXED_NOW + timedelta(minutes=14),
                    as_of=FIXED_NOW + timedelta(minutes=1),
                )
                with self.assertRaisesRegex(
                    current.BSECurrentDelistingBlockedError, "stale"
                ):
                    current.validate_current_delisting_freshness(
                        cold, now=FIXED_NOW + timedelta(minutes=16)
                    )

    def test_cross_url_and_multi_challenge_redirects_fail_closed(self) -> None:
        specs, raw_by_url = _fake_pdf_fixture()
        for kwargs in (
            {"challenge_location": "https://evil.example/notice.pdf"},
            {"second_is_redirect": True},
        ):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                session = _Session(specs=specs, raw_by_url=raw_by_url, **kwargs)
                with (
                    patch.object(current, "NOTICE_SPECS", specs),
                    patch.object(current, "_extract_pdf_text", side_effect=_fake_pdf_extract),
                ):
                    client = current.BSECurrentDelistingClient(
                        cas=current.BSECurrentDelistingCAS(Path(directory)),
                        session=session,
                        clock=lambda: FIXED_NOW,
                    )
                    with self.assertRaises(current.BSECurrentDelistingBlockedError):
                        client.fetch_current()

    def test_challenge_rejects_unexpected_or_insecure_cookie(self) -> None:
        specs, raw_by_url = _fake_pdf_fixture()
        for cookie in (
            "OTHER=fixture-cookie; Path=/; Secure",
            "C3VK=fixture-cookie; Path=/",
        ):
            with self.subTest(cookie=cookie), tempfile.TemporaryDirectory() as directory:
                session = _Session(specs=specs, raw_by_url=raw_by_url)
                session._get_queues[specs[0].source_url][0].headers["Set-Cookie"] = cookie
                with (
                    patch.object(current, "NOTICE_SPECS", specs),
                    patch.object(current, "_extract_pdf_text", side_effect=_fake_pdf_extract),
                ):
                    client = current.BSECurrentDelistingClient(
                        cas=current.BSECurrentDelistingCAS(Path(directory)),
                        session=session,
                        clock=lambda: FIXED_NOW,
                    )
                    with self.assertRaises(current.BSECurrentDelistingBlockedError):
                        client.fetch_current()

    def test_catalogue_redirect_and_present_target_fail_closed(self) -> None:
        for kwargs in (
            {"catalogue_response_url": "https://www.bse.cn/redirected"},
            {"catalogue_codes": ("920001", "920305", "920999")},
            {"catalogue_codes": ("920001", "920002", "920002")},
        ):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(current.BSECurrentDelistingBlockedError):
                    self._build(directory, challenge_first=False, **kwargs)

    def test_manifest_rejects_forged_summary_missing_duplicate_and_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session, specs = self._build(directory)
            store = current.BSECurrentDelistingManifestStore(cas)
            forged_summary = replace(
                artifact.catalogue_pages[0],
                response_summary={
                    **artifact.catalogue_pages[0].response_summary,
                    "total_elements": 999,
                },
            )
            attacks = {
                "forged_summary": replace(
                    artifact,
                    catalogue_pages=(forged_summary, *artifact.catalogue_pages[1:]),
                ),
                "missing_page": replace(
                    artifact, catalogue_pages=artifact.catalogue_pages[:-1]
                ),
                "duplicate_page": replace(
                    artifact,
                    catalogue_pages=(
                        artifact.catalogue_pages[0],
                        artifact.catalogue_pages[0],
                    ),
                ),
                "replaced_notice": replace(
                    artifact,
                    notices=(
                        replace(
                            artifact.notices[0],
                            final_pdf=artifact.notices[1].final_pdf,
                        ),
                        artifact.notices[1],
                    ),
                ),
                "forged_event": replace(
                    artifact,
                    events=(
                        replace(artifact.events[0], effective_date="2099-01-01"),
                        artifact.events[1],
                    ),
                ),
                "forged_transport_audit": replace(
                    artifact,
                    notices=(
                        replace(
                            artifact.notices[0],
                            transport_attempts=(
                                replace(
                                    artifact.notices[0].transport_attempts[0],
                                    transport_audit_sha256="0" * 64,
                                ),
                                *artifact.notices[0].transport_attempts[1:],
                            ),
                        ),
                        artifact.notices[1],
                    ),
                ),
                "wrong_closure_probe": replace(
                    artifact,
                    catalogue_closure_probe=artifact.catalogue_pages[1],
                ),
            }
            with (
                patch.object(current, "NOTICE_SPECS", specs),
                patch.object(current, "BSE_CATALOGUE_PAGE_SIZE", 2),
                patch.object(current, "BSE_CATALOGUE_MINIMUM_ROWS", 3),
                patch.object(current, "_extract_pdf_text", side_effect=_fake_pdf_extract),
            ):
                for label, attacked in attacks.items():
                    with self.subTest(label=label):
                        with self.assertRaises(
                            current.BSECurrentDelistingBlockedError
                        ):
                            store.seal(attacked)

    def test_raw_cas_tamper_and_reparse_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session, specs = self._build(directory)
            with (
                patch.object(current, "NOTICE_SPECS", specs),
                patch.object(current, "BSE_CATALOGUE_PAGE_SIZE", 2),
                patch.object(current, "BSE_CATALOGUE_MINIMUM_ROWS", 3),
                patch.object(current, "_extract_pdf_text", side_effect=_fake_pdf_extract),
            ):
                store = current.BSECurrentDelistingManifestStore(cas)
                reference = store.seal(artifact)
                target = cas._blob_path(
                    artifact.catalogue_pages[0].raw_response.content_sha256
                )
                target.write_bytes(b"tampered")
                with self.assertRaisesRegex(
                    current.BSECurrentDelistingBlockedError, "hash or size"
                ):
                    store.replay(reference.manifest_sha256)

        with tempfile.TemporaryDirectory() as directory:
            cas = current.BSECurrentDelistingCAS(Path(directory))
            with patch.object(current, "_path_is_link_or_reparse", return_value=True):
                with self.assertRaisesRegex(
                    current.BSECurrentDelistingBlockedError, "reparse"
                ):
                    cas.put_blob(b"blocked")


if __name__ == "__main__":
    unittest.main()
