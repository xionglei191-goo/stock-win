from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import requests

from research_platform.sse_risk_warning_source import (
    SOURCE_CONTRACT_UNADMITTED,
    SOURCE_SPECS,
    SSERiskWarningManifestStore,
    SSERiskWarningRawCAS,
    SSERiskWarningSourceBlockedError,
    SSERiskWarningSourceClient,
    build_source_request_url,
    parse_source_response,
)


RETRIEVED_AT = "2026-08-13T12:00:00+08:00"


def _rows_for(source_index: int) -> list[dict[str, str]]:
    if source_index == 0:
        return [
            {"INSTRUMENT_SHORT": "*ST九鼎", "INSTRUMENT_ID": "600053"},
            {"INSTRUMENT_SHORT": "ST中路B", "INSTRUMENT_ID": "900915"},
        ]
    return [
        {"secNameCn": "*ST瀚川", "secCode": "688022"},
        {"secNameCn": "*ST智翔", "secCode": "688443"},
    ]


def _payload(source_index: int) -> dict[str, object]:
    spec = SOURCE_SPECS[source_index]
    rows = _rows_for(source_index)
    return {
        "actionErrors": [],
        "actionMessages": [],
        "fieldErrors": {},
        "isPagination": "false",
        "jsonCallBack": spec.callback,
        "locale": "en",
        "pageHelp": {
            "beginPage": 0,
            "cacheSize": 1,
            "data": rows,
            "endDate": None,
            "endPage": None,
            "objectResult": None,
            "pageCount": 1,
            "pageNo": 1,
            "pageSize": len(rows),
            "pageSizeWithOutLimit": len(rows),
            "searchDate": None,
            "sort": None,
            "startDate": None,
            "total": len(rows),
        },
        "pageNo": None,
        "pageSize": None,
        "queryDate": "",
        "result": rows,
        "securityCode": "",
        "sqlId": spec.sql_id,
        "texts": None,
        "type": spec.response_type,
        "validateCode": "",
    }


def _raw(source_index: int, payload: dict[str, object] | None = None) -> bytes:
    spec = SOURCE_SPECS[source_index]
    value = payload if payload is not None else _payload(source_index)
    body = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{spec.callback}({body})".encode("utf-8")


class _Response:
    def __init__(
        self,
        *,
        url: str,
        content: bytes,
        status_code: int = 200,
        content_type: str = "application/json;charset=UTF-8",
    ) -> None:
        self.url = url
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class _FakeSession:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        return self.responses[url]

    def post(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("risk-warning source must never issue POST")


class _TimeoutSession:
    def get(self, _url: str, **_kwargs: object) -> _Response:
        raise requests.Timeout("simulated timeout")


def _session(
    *,
    response_overrides: dict[int, dict[str, object]] | None = None,
) -> _FakeSession:
    overrides = response_overrides or {}
    responses: dict[str, _Response] = {}
    for index, spec in enumerate(SOURCE_SPECS):
        url = build_source_request_url(spec)
        options = dict(overrides.get(index, {}))
        responses[url] = _Response(url=url, content=_raw(index), **options)
    return _FakeSession(responses)


class SSERiskWarningSourceTests(unittest.TestCase):
    def test_fetch_is_get_only_complete_and_persists_exact_raw_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _session()
            artifact = SSERiskWarningSourceClient(
                cas=SSERiskWarningRawCAS(Path(directory)),
                session=session,
            ).fetch_current(retrieved_at=RETRIEVED_AT)

            self.assertEqual(len(session.calls), 2)
            for _url, kwargs in session.calls:
                self.assertFalse(kwargs["allow_redirects"])
                self.assertEqual(kwargs["timeout"], 30.0)
                self.assertEqual(
                    kwargs["headers"]["Referer"],
                    "https://www.sse.com.cn/disclosure/listedinfo/riskplate/",
                )
            self.assertEqual(
                [item.code for item in artifact.securities],
                ["600053.SH", "688022.SH", "688443.SH", "900915.SH"],
            )
            self.assertEqual(
                [item.share_class for item in artifact.securities],
                ["A", "A", "A", "B"],
            )
            self.assertEqual(
                artifact.source_totals,
                {
                    "MAIN_BOARD_RISK_WARNING": 2,
                    "STAR_MARKET_RISK_WARNING": 2,
                },
            )
            self.assertTrue(artifact.source_contract["ready"])
            self.assertEqual(
                artifact.source_contract["pagination_mode"],
                "SERVER_DECLARED_UNPAGINATED_FULL_RESPONSE",
            )
            self.assertEqual(artifact.statistics["a_share_rows"], 3)
            self.assertEqual(
                artifact.statistics["b_share_rows_excluded_from_a_share_set"], 1
            )
            self.assertEqual(
                artifact.statistics["a_share_code_set_sha256"],
                hashlib.sha256(
                    json.dumps(
                        ["600053.SH", "688022.SH", "688443.SH"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            )
            for index, evidence in enumerate(artifact.raw_responses):
                expected = _raw(index)
                self.assertEqual(evidence.method, "GET")
                self.assertEqual(evidence.retrieved_at, RETRIEVED_AT)
                self.assertEqual(
                    evidence.content_sha256,
                    hashlib.sha256(expected).hexdigest(),
                )
                self.assertEqual(Path(evidence.object_path).read_bytes(), expected)
                self.assertEqual(evidence.cas_uri, f"sha256:{evidence.content_sha256}")

    def test_canonical_manifest_replays_both_raw_sources_from_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = SSERiskWarningRawCAS(Path(directory))
            artifact = SSERiskWarningSourceClient(
                cas=cas,
                session=_session(),
            ).fetch_current(retrieved_at=RETRIEVED_AT)
            store = SSERiskWarningManifestStore(cas)
            reference = store.seal(artifact)
            manifest_bytes = Path(reference.object_path).read_bytes()
            self.assertEqual(
                hashlib.sha256(manifest_bytes).hexdigest(),
                reference.manifest_sha256,
            )
            self.assertEqual(
                manifest_bytes,
                json.dumps(
                    json.loads(manifest_bytes),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8"),
            )
            replayed = store.replay(reference.manifest_sha256)
            self.assertEqual(replayed.to_dict(), artifact.to_dict())

            tampered_payload = json.loads(manifest_bytes)
            tampered_payload["statistics"]["a_share_rows"] += 1
            tampered_manifest = json.dumps(
                tampered_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            tampered_hash, _tampered_path = cas.put_blob(tampered_manifest)
            with self.assertRaisesRegex(
                SSERiskWarningSourceBlockedError,
                "statistics do not match raw bytes",
            ):
                store.replay(tampered_hash)

            first_raw = replayed.raw_responses[0]
            raw_path = Path(first_raw.object_path)
            raw_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(
                SSERiskWarningSourceBlockedError, "CAS object hash mismatch"
            ):
                store.replay(reference.manifest_sha256)

    def test_expected_hashes_bind_each_exact_source_response(self) -> None:
        expected = {
            spec.source_id: hashlib.sha256(_raw(index)).hexdigest()
            for index, spec in enumerate(SOURCE_SPECS)
        }
        with tempfile.TemporaryDirectory() as directory:
            client = SSERiskWarningSourceClient(
                cas=SSERiskWarningRawCAS(Path(directory)),
                session=_session(),
            )
            artifact = client.fetch_current(
                retrieved_at=RETRIEVED_AT,
                expected_hashes=expected,
            )
            self.assertEqual(len(artifact.raw_responses), 2)
            bad = dict(expected)
            bad[SOURCE_SPECS[0].source_id] = "0" * 64
            with self.assertRaisesRegex(
                SSERiskWarningSourceBlockedError, "source hash mismatch"
            ):
                client.fetch_current(
                    retrieved_at=RETRIEVED_AT,
                    expected_hashes=bad,
                )
            with self.assertRaisesRegex(
                SSERiskWarningSourceBlockedError, "unknown expected source hashes"
            ):
                client.fetch_current(
                    retrieved_at=RETRIEVED_AT,
                    expected_hashes={"CALLER_SAYS_OK": "0" * 64},
                )

    def test_empty_or_changed_pagination_contract_fails_closed(self) -> None:
        spec = SOURCE_SPECS[0]

        empty = _payload(0)
        empty["result"] = []
        page = empty["pageHelp"]
        assert isinstance(page, dict)
        page["data"] = []
        page["total"] = 0
        page["pageSize"] = 0
        page["pageSizeWithOutLimit"] = 0
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "returned an empty list"
        ):
            parse_source_response(_raw(0, empty), spec=spec)

        paginated = _payload(0)
        page = paginated["pageHelp"]
        assert isinstance(page, dict)
        paginated["isPagination"] = "true"
        page["pageCount"] = 2
        with self.assertRaises(SSERiskWarningSourceBlockedError) as raised:
            parse_source_response(_raw(0, paginated), spec=spec)
        self.assertEqual(raised.exception.status, SOURCE_CONTRACT_UNADMITTED)

        limited = _payload(0)
        page = limited["pageHelp"]
        assert isinstance(page, dict)
        page["pageSizeWithOutLimit"] = 3
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "upper-limit closure failed"
        ):
            parse_source_response(_raw(0, limited), spec=spec)

    def test_missed_rows_duplicates_order_and_schema_drift_fail_closed(self) -> None:
        spec = SOURCE_SPECS[0]

        missed = _payload(0)
        page = missed["pageHelp"]
        assert isinstance(page, dict)
        page["data"] = list(_rows_for(0)[:1])
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "result and pageHelp.data diverged"
        ):
            parse_source_response(_raw(0, missed), spec=spec)

        duplicate = _payload(0)
        rows = list(_rows_for(0))
        rows[1] = dict(rows[0])
        duplicate["result"] = rows
        page = duplicate["pageHelp"]
        assert isinstance(page, dict)
        page["data"] = rows
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "duplicate code"
        ):
            parse_source_response(_raw(0, duplicate), spec=spec)

        reversed_rows = _payload(0)
        rows = list(reversed(_rows_for(0)))
        reversed_rows["result"] = rows
        page = reversed_rows["pageHelp"]
        assert isinstance(page, dict)
        page["data"] = rows
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "code order changed"
        ):
            parse_source_response(_raw(0, reversed_rows), spec=spec)

        extra_field = _payload(0)
        rows = [dict(item) for item in _rows_for(0)]
        rows[0]["UNEXPECTED"] = "x"
        extra_field["result"] = rows
        page = extra_field["pageHelp"]
        assert isinstance(page, dict)
        page["data"] = rows
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "row schema drift"
        ):
            parse_source_response(_raw(0, extra_field), spec=spec)

        top_level_drift = _payload(0)
        top_level_drift["unexpected"] = True
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "top-level schema drift"
        ):
            parse_source_response(_raw(0, top_level_drift), spec=spec)

    def test_callback_sql_errors_and_duplicate_json_keys_fail_closed(self) -> None:
        spec = SOURCE_SPECS[0]
        payload = _payload(0)
        payload["actionMessages"] = ["warning"]
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "API errors or messages"
        ):
            parse_source_response(_raw(0, payload), spec=spec)

        payload = _payload(0)
        payload["sqlId"] = "OTHER_SQL"
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "SQL contract mismatch"
        ):
            parse_source_response(_raw(0, payload), spec=spec)

        wrong_wrapper = _raw(0).replace(spec.callback.encode(), b"wrongCallback", 1)
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "JSONP wrapper changed"
        ):
            parse_source_response(wrong_wrapper, spec=spec)

        duplicate_key = (
            f'{spec.callback}({{"actionErrors":[],"actionErrors":[]}})'
        ).encode()
        with self.assertRaisesRegex(
            SSERiskWarningSourceBlockedError, "duplicate JSON key"
        ):
            parse_source_response(duplicate_key, spec=spec)

    def test_http_host_content_type_and_status_are_strict(self) -> None:
        first_url = build_source_request_url(SOURCE_SPECS[0])
        cases = (
            (
                {0: {"status_code": 503}},
                "HTTP 503",
            ),
            (
                {0: {"content_type": "text/html"}},
                "content type changed",
            ),
        )
        for overrides, pattern in cases:
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as directory:
                client = SSERiskWarningSourceClient(
                    cas=SSERiskWarningRawCAS(Path(directory)),
                    session=_session(response_overrides=overrides),
                )
                with self.assertRaisesRegex(SSERiskWarningSourceBlockedError, pattern):
                    client.fetch_current(retrieved_at=RETRIEVED_AT)

        redirect_session = _session()
        redirect_session.responses[first_url].url = (
            "https://evil.invalid/commonSoaQuery.do?jsonCallBack=x"
        )
        with tempfile.TemporaryDirectory() as directory:
            client = SSERiskWarningSourceClient(
                cas=SSERiskWarningRawCAS(Path(directory)),
                session=redirect_session,
            )
            with self.assertRaisesRegex(
                SSERiskWarningSourceBlockedError, "request origin changed"
            ):
                client.fetch_current(retrieved_at=RETRIEVED_AT)

        with tempfile.TemporaryDirectory() as directory:
            client = SSERiskWarningSourceClient(
                cas=SSERiskWarningRawCAS(Path(directory)),
                session=_TimeoutSession(),  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(
                SSERiskWarningSourceBlockedError, "GET failed closed"
            ):
                client.fetch_current(retrieved_at=RETRIEVED_AT)

    def test_unadmitted_request_and_corrupt_existing_cas_fail_closed(self) -> None:
        unadmitted = replace(
            SOURCE_SPECS[0],
            query_items=(("domesticIndicator", "P"), ("productType", "0")),
        )
        with self.assertRaises(SSERiskWarningSourceBlockedError) as raised:
            build_source_request_url(unadmitted)
        self.assertEqual(raised.exception.status, SOURCE_CONTRACT_UNADMITTED)

        with tempfile.TemporaryDirectory() as directory:
            raw = _raw(0)
            digest = hashlib.sha256(raw).hexdigest()
            path = Path(directory) / "sha256" / digest[:2] / digest
            path.parent.mkdir(parents=True)
            path.write_bytes(b"corrupt")
            client = SSERiskWarningSourceClient(
                cas=SSERiskWarningRawCAS(Path(directory)),
                session=_session(),
            )
            with self.assertRaisesRegex(
                SSERiskWarningSourceBlockedError,
                "content-address collision or corruption",
            ):
                client.fetch_current(retrieved_at=RETRIEVED_AT)


if __name__ == "__main__":
    unittest.main()
