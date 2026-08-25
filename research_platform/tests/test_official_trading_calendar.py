from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from research_platform import official_trading_calendar as calendar


FIXED_NOW = datetime(2026, 8, 13, 9, 0, tzinfo=timezone(timedelta(hours=8)))


def _interval_text(spec: calendar.CalendarSourceSpec) -> str:
    values: list[str] = []
    for start_text, end_text in spec.expected_intervals:
        start = datetime.strptime(start_text, "%Y-%m-%d").date()
        end = datetime.strptime(end_text, "%Y-%m-%d").date()
        start_value = f"{start.year}年{start.month}月{start.day}日（星期一）"
        if start == end:
            values.append(f"{start_value}休市")
        else:
            values.append(
                f"{start_value}至{end.year}年{end.month}月{end.day}日"
                "（星期二）休市"
            )
    return "。".join(values)


def _source_fixture(spec: calendar.CalendarSourceSpec) -> bytes:
    content = (
        "<p>"
        + spec.title
        + "。"
        + "。".join(spec.required_markers)
        + "。"
        + _interval_text(spec)
        + "。</p>"
    )
    if spec.exchange == "SSE":
        value = {
            "releaseDate": f"{spec.publication_date} 09:30:00",
            "docId": spec.document_id,
            "publishdate": f"{spec.publication_date} 09:30:00",
            "title": spec.title,
            "url": spec.page_url,
            "content": content,
        }
    else:
        publication = datetime.combine(
            datetime.strptime(spec.publication_date, "%Y-%m-%d").date(),
            time.min,
            tzinfo=calendar.CHINA_STANDARD_TIME,
        )
        value = {
            "code": 0,
            "message": "成功",
            "data": {
                "channelId": 212,
                "chnlCode": "general_news",
                "content": content,
                "docAbstract": "",
                "docId": int(spec.document_id),
                "docTitleStatusTime": "",
                "domain": "https://www.szse.cn",
                "jsonPath": urlsplit(spec.request_url).path,
                "pubTime": int(publication.timestamp() * 1000),
                "title": spec.title,
                "url": urlsplit(spec.page_url).path,
            },
        }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


_CATALOG_EVENTS = (
    (2017, "ANNUAL"),
    (2017, "SPRING"),
    (2017, "QINGMING"),
    (2017, "LABOUR"),
    (2017, "DRAGON_BOAT"),
    (2017, "COMBINED"),
    (2018, "ANNUAL"),
    (2018, "SPRING"),
    (2018, "QINGMING"),
    (2018, "LABOUR"),
    (2018, "DRAGON_BOAT"),
    (2018, "MID_AUTUMN"),
    (2018, "NATIONAL_DAY"),
    (2019, "ANNUAL"),
    (2019, "SPRING"),
    (2019, "QINGMING"),
    (2019, "LABOUR_ADJUSTMENT"),
    (2019, "DRAGON_BOAT"),
    (2019, "MID_AUTUMN"),
    (2019, "NATIONAL_DAY"),
    (2020, "ANNUAL"),
    (2020, "SPRING"),
    (2020, "SPRING_EXTENSION"),
    (2020, "QINGMING"),
    (2020, "LABOUR"),
    (2020, "DRAGON_BOAT"),
    (2020, "COMBINED"),
    (2021, "ANNUAL"),
    (2021, "SPRING"),
    (2021, "QINGMING"),
    (2021, "LABOUR"),
    (2021, "DRAGON_BOAT"),
    (2021, "MID_AUTUMN"),
    (2021, "NATIONAL_DAY"),
    (2022, "ANNUAL"),
    (2022, "SPRING"),
    (2022, "QINGMING"),
    (2022, "LABOUR"),
    (2022, "DRAGON_BOAT"),
    (2022, "MID_AUTUMN"),
    (2022, "NATIONAL_DAY"),
    (2023, "ANNUAL"),
    (2023, "SPRING"),
    (2023, "QINGMING"),
    (2023, "LABOUR"),
    (2023, "DRAGON_BOAT"),
    (2023, "COMBINED"),
)

_SSE_CATALOG_IDENTITIES = (
    ("4218613", "2016-12-22", "20161222"),
    ("4227374", "2017-01-12", "20170112"),
    ("4255091", "2017-03-23", "20170322"),
    ("4277955", "2017-04-20", "20170419"),
    ("4312827", "2017-05-18", "20170518"),
    ("4392795", "2017-09-21", "20170921"),
    ("4438363", "2017-12-22", "20171222"),
    ("4461288", "2018-02-08", "20180208"),
    ("4477161", "2018-03-22", "20180320"),
    ("4504591", "2018-04-19", "20180417"),
    ("4567900", "2018-06-07", "20180606"),
    ("4643051", "2018-09-13", "20180912"),
    ("4646933", "2018-09-20", "20180919"),
    ("4696473", "2018-12-20", "20181220"),
    ("4714710", "2019-01-24", "20190124"),
    ("4745520", "2019-03-28", "20190326"),
    ("4771364", "2019-04-18", "20190418"),
    ("4827252", "2019-05-30", "20190528"),
    ("4909639", "2019-09-05", "20190903"),
    ("4917419", "2019-09-19", "20190919"),
    ("4969627", "2019-12-20", "20191220"),
    ("4985019", "2020-01-16", "20200115"),
    ("4991582", "2020-01-27", "20200127"),
    ("5013162", "2020-03-19", "20200319"),
    ("5040105", "2020-04-16", "20200416"),
    ("5123178", "2020-06-11", "20200611"),
    ("5220000", "2020-09-17", "20200917"),
    ("5286949", "2020-12-24", "20201224"),
    ("5317499", "2021-02-04", "20210204"),
    ("5348355", "2021-03-25", "20210325"),
    ("5386919", "2021-04-22", "20210421"),
    ("5481611", "2021-06-03", "20210603"),
    ("5585128", "2021-09-09", "20210909"),
    ("5595625", "2021-09-23", "20210923"),
    ("5662606", "2021-12-20", "20211220"),
    ("5688118", "2022-01-20", "20220120"),
    ("5700190", "2022-03-24", "20220324"),
    ("5701149", "2022-04-21", "20220420"),
    ("5702578", "2022-05-26", "20220526"),
    ("5707975", "2022-09-01", "20220901"),
    ("5709385", "2022-09-22", "20220922"),
    ("5714458", "2022-12-27", "20221227"),
    ("5715141", "2023-01-13", "20230113"),
    ("5718457", "2023-03-23", "20230323"),
    ("5720474", "2023-04-21", "20230421"),
    ("5722605", "2023-06-15", "20230615"),
    ("5726863", "2023-09-21", "20230920"),
)

_SZSE_CATALOG_IDENTITIES = (
    ("501883", "2016-12-22", "20161222"),
    ("501912", "2017-01-12", "20170112"),
    ("501985", "2017-03-23", "20170323"),
    ("502015", "2017-04-20", "20170420"),
    ("502050", "2017-05-18", "20170518"),
    ("502167", "2017-09-21", "20170921"),
    ("502255", "2017-12-22", "20171222"),
    ("502319", "2018-02-08", "20180208"),
    ("502346", "2018-03-22", "20180322"),
    ("535000", "2018-04-19", "20180419"),
    ("539222", "2018-06-07", "20180607"),
    ("554978", "2018-09-13", "20180913"),
    ("555433", "2018-09-20", "20180920"),
    ("563695", "2018-12-20", "20181220"),
    ("564355", "2019-01-24", "20190124"),
    ("565825", "2019-03-28", "20190328"),
    ("566376", "2019-04-18", "20190418"),
    ("567531", "2019-05-30", "20190530"),
    ("570444", "2019-09-05", "20190905"),
    ("570797", "2019-09-19", "20190919"),
    ("572766", "2019-12-20", "20191220"),
    ("573576", "2020-01-16", "20200116"),
    ("573917", "2020-01-27", "20200127"),
    ("575259", "2020-03-19", "20200319"),
    ("576025", "2020-04-16", "20200416"),
    ("578291", "2020-06-11", "20200611"),
    ("581610", "2020-09-17", "20200917"),
    ("583950", "2020-12-24", "20201224"),
    ("584680", "2021-02-04", "20210204"),
    ("585260", "2021-03-25", "20210325"),
    ("585626", "2021-04-22", "20210422"),
    ("586257", "2021-06-03", "20210603"),
    ("588245", "2021-09-09", "20210909"),
    ("588557", "2021-09-23", "20210923"),
    ("590321", "2021-12-20", "20211220"),
    ("590855", "2022-01-20", "20220120"),
    ("592024", "2022-03-24", "20220324"),
    ("592505", "2022-04-21", "20220421"),
    ("593440", "2022-05-26", "20220526"),
    ("595669", "2022-09-01", "20220901"),
    ("596099", "2022-09-22", "20220922"),
    ("598022", "2022-12-27", "20221227"),
    ("598300", "2023-01-13", "20230113"),
    ("599411", "2023-03-23", "20230323"),
    ("600057", "2023-04-21", "20230421"),
    ("601166", "2023-06-15", "20230615"),
    ("603664", "2023-09-21", "20230921"),
)


def _catalog_title(exchange: str, year: int, event: str) -> str:
    suffix = "公告" if exchange == "SSE" else "通知"
    if event == "ANNUAL":
        if exchange == "SSE":
            return f"关于上海证券交易所{year}年全年休市安排的通知"
        return f"关于{year}年部分节假日休市安排的通知"
    if event == "LABOUR_ADJUSTMENT":
        return f"关于调整{year}年劳动节休市安排的{suffix}"
    if event == "SPRING_EXTENSION":
        if exchange == "SSE":
            return "关于调整2020年春节休市相关安排的公告"
        return "关于延长2020年春节休市安排的通知"
    holidays = {
        "SPRING": "春节",
        "QINGMING": "清明节",
        "LABOUR": "劳动节",
        "DRAGON_BOAT": "端午节",
        "MID_AUTUMN": "中秋节",
        "NATIONAL_DAY": "国庆节",
        "COMBINED": (
            "国庆节、中秋节" if year in {2017, 2020} and exchange == "SSE"
            else "国庆节、中秋节" if year == 2020
            else "中秋节、国庆节"
        ),
    }
    return f"关于{year}年{holidays[event]}休市安排的{suffix}"


def _catalog_entries(exchange: str) -> tuple[calendar.CalendarCatalogEntry, ...]:
    identities = (
        _SSE_CATALOG_IDENTITIES if exchange == "SSE" else _SZSE_CATALOG_IDENTITIES
    )
    values: list[calendar.CalendarCatalogEntry] = []
    for (year, event), (document_id, published, path_date) in zip(
        _CATALOG_EVENTS, identities, strict=True
    ):
        if exchange == "SSE":
            page_path = (
                f"/disclosure/announcement/general/c/c_{path_date}_{document_id}.shtml"
            )
            json_path = page_path[:-6] + ".json"
        else:
            page_path = f"/disclosure/notice/general/t{path_date}_{document_id}.html"
            json_path = page_path[:-5] + ".json"
        values.append(
            calendar.CalendarCatalogEntry(
                exchange=exchange,
                document_id=document_id,
                publication_date=published,
                title=_catalog_title(exchange, year, event),
                page_path=page_path,
                json_path=json_path,
            )
        )
    return tuple(values)


def _sse_catalog_fixture(
    spec: calendar.CalendarCatalogSpec,
    entries: tuple[calendar.CalendarCatalogEntry, ...],
) -> bytes:
    values: list[dict[str, object]] = []
    for index, entry in enumerate(entries, start=1):
        created = f"{entry.publication_date} 17:00:00"
        values.append(
            {
                "id": index,
                "documentId": f"CMS{entry.document_id}_28_8319",
                "url": None,
                "title": entry.title.replace("休市", "<em>休市</em>"),
                "rtfContent": None,
                "createTime": created,
                "updateTime": created,
                "updateUserId": None,
                "updateUserName": None,
                "authorId": None,
                "author": None,
                "pageviews": 0,
                "textSummarization": None,
                "paperDocType": None,
                "documentType": None,
                "tagList": None,
                "shareCount": 0,
                "likeCount": 0,
                "disLikeCount": 0,
                "collectionCount": 0,
                "spaceId": 3,
                "spaceName": "上交所中文网站",
                "folderFullPath": None,
                "newFlag": None,
                "score": 1,
                "channelIdList": [8319],
                "extend": [
                    {"name": "PARENT_CURL", "value": None},
                    {"name": "PARENT_TITLE", "value": None},
                    {"name": "CCHANNELCODE", "value": "8319"},
                    {"name": "CSITECODE", "value": "28"},
                    {"name": "CURL", "value": entry.page_path},
                    {"name": "GSJC", "value": None},
                    {"name": "ORGBULLETINTYPE", "value": None},
                    {"name": "PARENT_DOC_CODE", "value": None},
                    {"name": "ZQDM", "value": None},
                    {"name": "FILETYPE", "value": "shtml"},
                ],
                "rule": None,
            }
        )
    params = dict(spec.request_params)
    payload = {
        "channelCode": params["channelCode"],
        "code": "0",
        "data": {
            "limit": calendar.CATALOG_PAGE_SIZE,
            "page": 0,
            "totalSize": len(values),
            "totalPage": 1,
            "costTime": 1,
            "correctionKeyword": None,
            "originKeyword": calendar.CATALOG_KEYWORD,
            "knowledgeList": values,
        },
        "docType": None,
        "keyword": params["keyword"],
        "keywordPosition": params["keywordPosition"],
        "limit": calendar.CATALOG_PAGE_SIZE,
        "msg": "调用成功",
        "orderByDirection": None,
        "orderByKey": None,
        "page": 0,
        "publishTimeEnd": params["publishTimeEnd"],
        "publishTimeStart": params["publishTimeStart"],
        "spaceId": 3,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _szse_catalog_fixture(
    entries: tuple[calendar.CalendarCatalogEntry, ...],
) -> bytes:
    values: list[dict[str, object]] = []
    for entry in entries:
        published = datetime.combine(
            datetime.strptime(entry.publication_date, "%Y-%m-%d").date(),
            time(17, 0),
            tzinfo=calendar.CHINA_STANDARD_TIME,
        )
        values.append(
            {
                "id": entry.document_id,
                "doctitle": entry.title.replace("休市", '<span class="keyword">休市</span>'),
                "doccontent": "官方目录摘要",
                "docpuburl": f"http://www.szse.cn{entry.page_path}",
                "docpubjsonurl": entry.json_path,
                "docpubtime": int(published.timestamp() * 1000),
                "doctype": "html",
                "chnlcode": "general_news",
                "index": "document",
                "navigation": "本所公告-通知公告",
                "attachPath": None,
                "subDocTitle": None,
                "docPeople": None,
                "docAbstract": None,
                "docTitleStatusTime": "",
                "docResolveContent": None,
            }
        )
    payload = {
        "totalSize": len(values),
        "data": values,
        "pageSize": calendar.CATALOG_PAGE_SIZE,
        "currentPage": 1,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


class _Response:
    def __init__(
        self,
        url: str,
        content: bytes,
        *,
        status_code: int = 200,
        content_type: str = "application/json;charset=UTF-8",
        response_url: str | None = None,
        location: str | None = None,
        history: tuple[object, ...] = (),
    ) -> None:
        self.url = response_url or url
        self.content = content
        self.status_code = status_code
        self.history = history
        self.headers: dict[str, str] = {"Content-Type": content_type}
        if location is not None:
            self.headers["Location"] = location


class _Session:
    def __init__(self, raw_by_url: dict[str, bytes]) -> None:
        self.raw_by_url = dict(raw_by_url)
        self.calls: list[dict[str, object]] = []
        self.overrides: dict[str, _Response | Exception] = {}

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        override = self.overrides.get(url)
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return override
        response_url = url
        if "params" in kwargs:
            value = requests.Request("GET", url, params=kwargs["params"]).prepare().url
            assert value is not None
            response_url = value
        return _Response(url, self.raw_by_url[url], response_url=response_url)

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        override = self.overrides.get(url)
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return override
        return _Response(url, self.raw_by_url[url])


def _fixture_session() -> _Session:
    raw_by_url = {
        spec.request_url: _source_fixture(spec) for spec in calendar.SOURCE_SPECS
    }
    sse_spec = calendar.CATALOG_BY_ID["SSE_CATALOG_2017_2023"]
    raw_by_url[sse_spec.request_url] = _sse_catalog_fixture(
        sse_spec, _catalog_entries("SSE")
    )
    szse_spec = calendar.CATALOG_BY_ID["SZSE_CATALOG_2017_2023"]
    raw_by_url[szse_spec.request_url] = _szse_catalog_fixture(
        _catalog_entries("SZSE")
    )
    return _Session(raw_by_url)


class OfficialTradingCalendarTests(unittest.TestCase):
    def test_fetch_builds_expected_calendar_and_cold_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _fixture_session()
            cas = calendar.OfficialTradingCalendarCAS(Path(directory))
            artifact = calendar.OfficialTradingCalendarClient(
                cas=cas,
                session=session,
                clock=lambda: FIXED_NOW,
            ).fetch()

            self.assertEqual(artifact.statistics["row_count"], 5_112)
            self.assertEqual(artifact.statistics["source_count"], 18)
            self.assertEqual(artifact.statistics["catalog_source_count"], 2)
            self.assertEqual(artifact.statistics["catalog_entry_count"], 94)
            self.assertEqual(
                artifact.source_contract["catalog_membership_sha256"],
                dict(calendar.EXPECTED_CATALOG_MEMBERSHIP_SHA256),
            )
            for year, expected in calendar.EXPECTED_OPEN_DAYS_BY_YEAR.items():
                for exchange in calendar.EXCHANGES:
                    self.assertEqual(
                        artifact.statistics["by_year"][str(year)][exchange][
                            "open_days"
                        ],
                        expected,
                    )

            by_key = {
                (item.exchange, item.trade_date): item for item in artifact.rows
            }
            for exchange in calendar.EXCHANGES:
                self.assertFalse(by_key[(exchange, "2019-05-02")].is_open)
                self.assertFalse(by_key[(exchange, "2019-05-03")].is_open)
                self.assertFalse(by_key[(exchange, "2020-01-31")].is_open)
                self.assertTrue(by_key[(exchange, "2020-02-03")].is_open)
                self.assertTrue(by_key[(exchange, "2023-04-06")].is_open)

            self.assertEqual(
                len(session.calls),
                len(calendar.SOURCE_SPECS) + len(calendar.CATALOG_SPECS),
            )
            for call, spec in zip(
                session.calls[: len(calendar.SOURCE_SPECS)],
                calendar.SOURCE_SPECS,
                strict=True,
            ):
                self.assertIs(call["allow_redirects"], False)
                self.assertEqual(call["headers"]["Referer"], spec.page_url)
                raw = session.raw_by_url[spec.request_url]
                source = next(
                    item
                    for item in artifact.raw_sources
                    if item.source_id == spec.source_id
                )
                self.assertEqual(
                    cas.read_blob(source.content_sha256)[0],
                    raw,
                )
            for call, spec in zip(
                session.calls[len(calendar.SOURCE_SPECS) :],
                calendar.CATALOG_SPECS,
                strict=True,
            ):
                self.assertIs(call["allow_redirects"], False)
                self.assertEqual(call["headers"]["Referer"], spec.referer_url)
                parameter_name = "params" if spec.method == "GET" else "data"
                self.assertEqual(call[parameter_name], list(spec.request_params))
                source = next(
                    item
                    for item in artifact.catalog_sources
                    if item.source_id == spec.source_id
                )
                self.assertEqual(
                    cas.read_blob(source.content_sha256)[0],
                    session.raw_by_url[spec.request_url],
                )

            reference = calendar.OfficialTradingCalendarManifestStore(cas).seal(
                artifact
            )
            replay = calendar.OfficialTradingCalendarManifestStore(
                calendar.OfficialTradingCalendarCAS(Path(directory))
            ).replay(reference.manifest_sha256)
            self.assertEqual(
                replay.logical_content_sha256, artifact.logical_content_sha256
            )
            self.assertEqual(replay.rows, artifact.rows)
            self.assertEqual(replay.catalog_entries, artifact.catalog_entries)

    def test_transport_and_incomplete_source_fail_closed(self) -> None:
        first = calendar.SOURCE_SPECS[0]
        attacks = {
            "redirect": _Response(
                first.request_url,
                _source_fixture(first),
                response_url="https://www.sse.com.cn/redirected.json",
            ),
            "history": _Response(
                first.request_url,
                _source_fixture(first),
                history=(object(),),
            ),
            "media_type": _Response(
                first.request_url,
                _source_fixture(first),
                content_type="text/html",
            ),
            "status": _Response(
                first.request_url,
                b"unavailable",
                status_code=503,
            ),
            "missing": requests.ConnectionError("offline"),
        }
        for label, response in attacks.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                session = _fixture_session()
                session.overrides[first.request_url] = response
                client = calendar.OfficialTradingCalendarClient(
                    cas=calendar.OfficialTradingCalendarCAS(Path(directory)),
                    session=session,
                    clock=lambda: FIXED_NOW,
                )
                with self.assertRaises(calendar.OfficialTradingCalendarBlockedError):
                    client.fetch()
                probe = client.probe()
                self.assertFalse(probe["ready"])
                self.assertIn(
                    probe["status"],
                    {calendar.SOURCE_INCOMPLETE, calendar.SOURCE_REJECTED},
                )

    def test_schema_duplicate_key_and_interval_drift_are_rejected(self) -> None:
        first = calendar.SOURCE_SPECS[0]
        original = json.loads(_source_fixture(first))
        attacks: dict[str, bytes] = {}

        schema = dict(original)
        schema["unexpected"] = True
        attacks["schema"] = json.dumps(schema, ensure_ascii=False).encode("utf-8")

        duplicate = _source_fixture(first).decode("utf-8")
        duplicate = duplicate.replace(
            '"docId":"4218613"',
            '"docId":"4218613","docId":"4218613"',
            1,
        )
        attacks["duplicate_key"] = duplicate.encode("utf-8")

        changed = dict(original)
        changed["content"] = changed["content"].replace(
            "2017年1月2日", "2017年1月3日", 1
        )
        attacks["interval"] = json.dumps(changed, ensure_ascii=False).encode("utf-8")

        for label, raw in attacks.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                session = _fixture_session()
                session.raw_by_url[first.request_url] = raw
                with self.assertRaises(calendar.OfficialTradingCalendarBlockedError):
                    calendar.OfficialTradingCalendarClient(
                        cas=calendar.OfficialTradingCalendarCAS(Path(directory)),
                        session=session,
                        clock=lambda: FIXED_NOW,
                    ).fetch()

    def test_catalog_missing_extra_schema_and_query_drift_fail_closed(self) -> None:
        sse = calendar.CATALOG_BY_ID["SSE_CATALOG_2017_2023"]
        attacks: dict[str, bytes] = {}
        original = json.loads(_sse_catalog_fixture(sse, _catalog_entries("SSE")))

        missing = json.loads(json.dumps(original, ensure_ascii=False))
        missing["data"]["knowledgeList"].pop()
        missing["data"]["totalSize"] -= 1
        attacks["missing"] = calendar._canonical_json_bytes(missing)

        extra = json.loads(json.dumps(original, ensure_ascii=False))
        copied = dict(extra["data"]["knowledgeList"][-1])
        copied["documentId"] = "CMS9999999_28_8319"
        copied["extend"] = [dict(item) for item in copied["extend"]]
        next(item for item in copied["extend"] if item["name"] == "CURL")[
            "value"
        ] = "/disclosure/announcement/general/c/c_20230921_9999999.shtml"
        extra["data"]["knowledgeList"].append(copied)
        extra["data"]["totalSize"] += 1
        attacks["extra"] = calendar._canonical_json_bytes(extra)

        schema = json.loads(json.dumps(original, ensure_ascii=False))
        schema["unexpected"] = True
        attacks["schema"] = calendar._canonical_json_bytes(schema)

        query = json.loads(json.dumps(original, ensure_ascii=False))
        query["publishTimeEnd"] = "2023-12-31 23:59:59"
        attacks["query"] = calendar._canonical_json_bytes(query)

        membership = json.loads(json.dumps(original, ensure_ascii=False))
        membership["data"]["knowledgeList"][1]["title"] = (
            "关于2017年春节休市安排的补充公告"
        )
        attacks["membership"] = calendar._canonical_json_bytes(membership)

        for label, raw in attacks.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                session = _fixture_session()
                session.raw_by_url[sse.request_url] = raw
                with self.assertRaises(calendar.OfficialTradingCalendarBlockedError):
                    calendar.OfficialTradingCalendarClient(
                        cas=calendar.OfficialTradingCalendarCAS(Path(directory)),
                        session=session,
                        clock=lambda: FIXED_NOW,
                    ).fetch()

    def test_catalog_cross_exchange_disagreement_and_missing_input_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _fixture_session()
            szse = calendar.CATALOG_BY_ID["SZSE_CATALOG_2017_2023"]
            values = list(_catalog_entries("SZSE"))
            changed = replace(
                values[1],
                title="关于2017年中秋节休市安排的通知",
            )
            values[1] = changed
            session.raw_by_url[szse.request_url] = _szse_catalog_fixture(tuple(values))
            with patch.dict(
                calendar.EXPECTED_CATALOG_MEMBERSHIP_SHA256,
                {
                    "SZSE": calendar._sha256(
                        calendar._canonical_json_bytes(
                            [item.to_dict() for item in sorted(values, key=calendar._catalog_entry_key)]
                        )
                    )
                },
            ):
                with self.assertRaisesRegex(
                    calendar.OfficialTradingCalendarBlockedError,
                    "catalogs disagree",
                ):
                    calendar.OfficialTradingCalendarClient(
                        cas=calendar.OfficialTradingCalendarCAS(Path(directory)),
                        session=session,
                        clock=lambda: FIXED_NOW,
                    ).fetch()

        with tempfile.TemporaryDirectory() as directory:
            session = _fixture_session()
            sse = calendar.CATALOG_BY_ID["SSE_CATALOG_2017_2023"]
            values = tuple(
                item
                for item in _catalog_entries("SSE")
                if item.document_id != "4771364"
            )
            session.raw_by_url[sse.request_url] = _sse_catalog_fixture(sse, values)
            with self.assertRaises(calendar.OfficialTradingCalendarBlockedError):
                calendar.OfficialTradingCalendarClient(
                    cas=calendar.OfficialTradingCalendarCAS(Path(directory)),
                    session=session,
                    clock=lambda: FIXED_NOW,
                ).fetch()

    def test_manifest_rows_missing_source_and_raw_tamper_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = calendar.OfficialTradingCalendarCAS(Path(directory))
            artifact = calendar.OfficialTradingCalendarClient(
                cas=cas,
                session=_fixture_session(),
                clock=lambda: FIXED_NOW,
            ).fetch()
            store = calendar.OfficialTradingCalendarManifestStore(cas)
            reference = store.seal(artifact)
            manifest_raw, _path = cas.read_blob(reference.manifest_sha256)
            payload = json.loads(manifest_raw)

            forged = json.loads(manifest_raw)
            forged["rows"][0]["is_open"] = not forged["rows"][0]["is_open"]
            forged_digest, _ = cas.put_blob(calendar._canonical_json_bytes(forged))
            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "rows do not match",
            ):
                store.replay(forged_digest)

            missing = json.loads(manifest_raw)
            missing["sources"] = missing["sources"][:-1]
            missing_digest, _ = cas.put_blob(calendar._canonical_json_bytes(missing))
            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "incomplete",
            ):
                store.replay(missing_digest)

            missing_catalog = json.loads(manifest_raw)
            missing_catalog["catalog_sources"] = missing_catalog[
                "catalog_sources"
            ][:-1]
            missing_catalog_digest, _ = cas.put_blob(
                calendar._canonical_json_bytes(missing_catalog)
            )
            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "catalog evidence set is incomplete",
            ):
                store.replay(missing_catalog_digest)

            forged_catalog = json.loads(manifest_raw)
            forged_catalog["catalog_entries"][0]["title"] = "伪造休市安排"
            forged_catalog_digest, _ = cas.put_blob(
                calendar._canonical_json_bytes(forged_catalog)
            )
            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "catalog entries do not match raw evidence",
            ):
                store.replay(forged_catalog_digest)

            first_source = artifact.raw_sources[0]
            source_path = Path(first_source.object_path)
            source_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "hash mismatch",
            ):
                store.replay(reference.manifest_sha256)

        with tempfile.TemporaryDirectory() as directory:
            cas = calendar.OfficialTradingCalendarCAS(Path(directory))
            artifact = calendar.OfficialTradingCalendarClient(
                cas=cas,
                session=_fixture_session(),
                clock=lambda: FIXED_NOW,
            ).fetch()
            store = calendar.OfficialTradingCalendarManifestStore(cas)
            reference = store.seal(artifact)
            Path(artifact.catalog_sources[0].object_path).write_bytes(b"tampered")
            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "hash mismatch",
            ):
                store.replay(reference.manifest_sha256)

            self.assertEqual(payload["protocol_version"], calendar.PROTOCOL_VERSION)

    def test_symlink_reparse_and_read_time_change_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = calendar.OfficialTradingCalendarCAS(Path(directory))
            with patch.object(calendar, "_is_link_or_reparse", return_value=True):
                with self.assertRaisesRegex(
                    calendar.OfficialTradingCalendarBlockedError,
                    "symlink or reparse",
                ):
                    cas.put_blob(b"blocked")

        with tempfile.TemporaryDirectory() as directory:
            cas = calendar.OfficialTradingCalendarCAS(Path(directory))
            digest, path = cas.put_blob(b"stable")
            before = calendar._path_snapshot(cas.root, path, leaf_file=True)
            changed = list(before)
            leaf_name, fingerprint = changed[-1]
            changed[-1] = (
                leaf_name,
                (*fingerprint[:-3], fingerprint[-3] + 1, *fingerprint[-2:]),
            )
            with patch.object(
                calendar,
                "_path_snapshot",
                side_effect=[before, tuple(changed)],
            ):
                with self.assertRaisesRegex(
                    calendar.OfficialTradingCalendarBlockedError,
                    "changed during read",
                ):
                    cas.read_blob(digest)

    def test_existing_cas_directories_support_repeated_put_and_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "existing" / "cas"
            (root / "sha256").mkdir(parents=True)
            cas = calendar.OfficialTradingCalendarCAS(root)
            expected: dict[str, bytes] = {}
            for index in range(30):
                content = f"existing-cas-{index % 7}".encode()
                digest, _path = cas.put_blob(content)
                expected[digest] = content
            for digest, content in expected.items():
                self.assertEqual(cas.read_blob(digest)[0], content)

            artifact = calendar.OfficialTradingCalendarClient(
                cas=cas,
                session=_fixture_session(),
                clock=lambda: FIXED_NOW,
            ).fetch()
            store = calendar.OfficialTradingCalendarManifestStore(cas)
            first = store.seal(artifact)
            second = store.seal(artifact)
            self.assertEqual(first.manifest_sha256, second.manifest_sha256)
            self.assertEqual(
                store.replay(second.manifest_sha256).logical_content_sha256,
                artifact.logical_content_sha256,
            )

    def test_parent_directory_file_id_replacement_is_rejected(self) -> None:
        expected = (11, *range(16))
        replacement = (12, *range(16))
        with (
            patch.object(calendar, "_path_snapshot", return_value=()),
            patch.object(calendar, "_open_directory_handle", return_value=222),
            patch.object(
                calendar,
                "_directory_handle_identity",
                side_effect=[expected, replacement],
            ),
            patch.object(calendar, "_close_directory_handle") as close_handle,
        ):
            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "was replaced during write",
            ):
                calendar._verify_directory_identity(
                    Path("C:/cas"),
                    Path("C:/cas/sha256/aa"),
                    held_handle=111,
                    expected_identity=expected,
                )
            close_handle.assert_called_once_with(222)


if __name__ == "__main__":
    unittest.main()
