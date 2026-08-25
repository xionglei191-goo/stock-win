from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from research_platform.__main__ import build_parser
from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file
from research_platform.us_pit.sources import SyncRequest
from research_platform.us_pit.sources_official import HTTPResponse, SourceFetchError
from research_platform.us_pit.sources_spglobal import (
    SPGlobalSP500MembershipEventAdapter,
    parse_sp500_membership_announcement,
)
from research_platform.us_pit.spglobal_events import (
    build_spglobal_event_candidates,
    prepare_spglobal_event_review,
    reparse_spglobal_event_probes,
    review_spglobal_event_evidence,
)
from research_platform.us_pit.identity_bridge import normalized_issuer_name
from research_platform.us_pit.service import USPITService
from research_platform.us_pit.store import USPITStore


OBSERVED = datetime(2026, 8, 14, tzinfo=timezone.utc)


def _announcement(*, duplicate: bool = False) -> bytes:
    extra = "" if not duplicate else (
        "<tr><td></td><td>S&amp;P 500</td><td>Addition</td>"
        "<td>Apple Inc</td><td>AAPL</td><td>Technology</td></tr>"
    )
    return f"""<!doctype html><html><body>
<table><tr><td>Effective Date</td><td>Index Name</td><td>Action</td>
<td>Company Name</td><td>Ticker</td><td>GICS Sector</td></tr>
<tr><td>Dec. 20, 2021</td><td>S&amp;P 500</td><td>Addition</td>
<td>Apple Inc</td><td>AAPL</td><td>Technology</td></tr>
<tr><td></td><td>S&amp;P 500</td><td>Deletion</td>
<td>Old Corp</td><td>OLD</td><td>Industrials</td></tr>{extra}
<tr><td></td><td>S&amp;P MidCap 400</td><td>Addition</td>
<td>Old Corp</td><td>OLD</td><td>Industrials</td></tr></table>
<!-- ITEMDATE: 2021-12-03 19:52:00 EST -->
</body></html>""".encode()


def _archive(link: str | None) -> bytes:
    value = "" if link is None else f'<a href="{link}">Apple Set to Join S&amp;P 500</a>'
    return f"<!doctype html><html><body>{value}</body></html>".encode()


def _narrative_announcement(body: str, *, itemdate: str = "2020-05-06 19:23:00 EDT") -> bytes:
    return (
        f"<!doctype html><html><body><p>{body}</p>"
        f"<!-- ITEMDATE: {itemdate} --></body></html>"
    ).encode()


class _Transport:
    def __init__(self, link: str, *, announcement: bytes | None = None) -> None:
        self.link = link
        self.announcement = announcement or _announcement()
        self.calls: list[str] = []

    def get(self, url: str, *, headers, timeout: float) -> HTTPResponse:
        self.calls.append(url)
        query = parse_qs(urlparse(url).query)
        payload = (
            _archive(self.link if query.get("o", ["0"])[0] == "0" else None)
            if "index.php" in url
            else self.announcement
        )
        return HTTPResponse(url=url, status_code=200, content=payload, headers={"Content-Type": "text/html"})


class _RetryTransport(_Transport):
    def __init__(self, link: str) -> None:
        super().__init__(link)
        self.failures = 1

    def get(self, url: str, *, headers, timeout: float) -> HTTPResponse:
        if "index.php" not in url and self.failures:
            self.failures -= 1
            raise SourceFetchError("transient reset")
        return super().get(url, headers=headers, timeout=timeout)


class SPGlobalMembershipEventTests(unittest.TestCase):
    def test_official_issuer_name_normalization_handles_common_abbreviations(self) -> None:
        self.assertEqual(
            normalized_issuer_name("Charles River Laboratories Intl Inc."),
            normalized_issuer_name("Charles River Laboratories International"),
        )
        self.assertEqual(
            normalized_issuer_name("Apartment Investment & Mgt Co"),
            normalized_issuer_name("Apartment Investment and Management Company"),
        )
        self.assertEqual(
            normalized_issuer_name("NXP Semiconductors NV"),
            normalized_issuer_name("NXP Semiconductors"),
        )
        self.assertEqual(
            normalized_issuer_name("Organon & Co."),
            normalized_issuer_name("Organon"),
        )
    def test_parser_keeps_only_sp500_and_payload_publication_time(self) -> None:
        events = parse_sp500_membership_announcement(_announcement())
        self.assertEqual(2, len(events))
        self.assertEqual({"ADD", "REMOVE"}, {item.event_type for item in events})
        self.assertEqual(
            datetime(2021, 12, 3, 19, 52, tzinfo=events[0].announced_at.tzinfo),
            events[0].announced_at,
        )
        duplicated = parse_sp500_membership_announcement(_announcement(duplicate=True))
        self.assertEqual(2, len(duplicated))

    def test_parser_splits_explicit_multi_share_class_ticker(self) -> None:
        payload = _announcement().replace(b"<td>OLD</td>", b"<td>UA/UAA</td>")
        events = parse_sp500_membership_announcement(payload)
        removals = [item for item in events if item.event_type == "REMOVE"]
        self.assertEqual({"UA", "UAA"}, {item.ticker for item in removals})

    def test_parser_does_not_guess_a_tba_effective_date(self) -> None:
        payload = _announcement().replace(b"Dec. 20, 2021", b"TBA")
        self.assertEqual((), parse_sp500_membership_announcement(payload))

    def test_parser_accepts_explicit_narrative_replacement_without_a_table(self) -> None:
        payload = _narrative_announcement(
            "S&amp;P Dow Jones Indices will make the following changes effective "
            "prior to the opening on Tuesday, May 12: DexCom Inc. (NASD:DXCM) "
            "will replace Allergan plc (NYSE:AGN) in the S&amp;P 500."
        )
        events = parse_sp500_membership_announcement(payload)
        self.assertEqual(
            {("ADD", "DXCM", date(2020, 5, 12)), ("REMOVE", "AGN", date(2020, 5, 12))},
            {(item.event_type, item.ticker, item.effective_date) for item in events},
        )
        self.assertEqual(
            {"DexCom Inc.", "Allergan plc"}, {item.company_name for item in events}
        )

    def test_parser_strips_press_page_prefix_from_company_name(self) -> None:
        payload = _narrative_announcement(
            "Press Releases Paycom Software Set to Join S&amp;P 500 NEW YORK , "
            "Jan. 22, 2020 / PRNewswire / -- Paycom Software Inc. (NYSE:PAYC) "
            "will replace WellCare Health Plans Inc. (NYSE:WCG) in the S&amp;P 500 "
            "effective prior to the opening on Tuesday, Jan. 28."
        )
        events = parse_sp500_membership_announcement(payload)
        addition = next(item for item in events if item.event_type == "ADD")
        self.assertEqual("Paycom Software Inc.", addition.company_name)

    def test_parser_accepts_explicit_narrative_rebalance_pairing(self) -> None:
        payload = _narrative_announcement(
            "The changes will be effective prior to the open of trading on Monday, "
            "June 22. S&amp;P MidCap 400 constituents Tyler Technologies Inc. "
            "(NYSE:TYL), Bio-Rad Laboratories Inc. (NYSE:BIO) and Teledyne "
            "Technologies Inc. (NYSE:TDY) will move to the S&amp;P 500, replacing "
            "Harley-Davidson Inc. (NYSE:HOG), Nordstrom Inc. (NYSE:JWN) and "
            "Alliance Data Systems Corp. (NYSE:ADS), respectively.",
            itemdate="2020-06-12 18:18:00 EDT",
        )
        events = parse_sp500_membership_announcement(payload)
        self.assertEqual(6, len(events))
        self.assertEqual({"TYL", "BIO", "TDY"}, {x.ticker for x in events if x.event_type == "ADD"})
        self.assertEqual({"HOG", "JWN", "ADS"}, {x.ticker for x in events if x.event_type == "REMOVE"})

    def test_parser_handles_multi_replacement_with_split_effective_dates(self) -> None:
        payload = _narrative_announcement(
            "S&amp;P 500 and 100 constituent United Technologies Corp. (NYSE: UTX) "
            "is spinning off Otis Worldwide and Carrier Global. Otis Worldwide Corp. "
            "(NYSE: OTIS) and Carrier Global Corp. (NYSE: CARR) will be added to the "
            "S&amp;P 500 prior to the open of trading on Friday, April 3. Otis "
            "Worldwide will replace Raytheon Co. (NYSE: RTN), and Carrier Global "
            "will replace Macy's Inc. (NYSE: M) both of which will be removed from "
            "the S&amp;P 500 effective prior to the open of trading on Monday, April 6.",
            itemdate="2020-03-31 18:30:00 EDT",
        )
        events = parse_sp500_membership_announcement(payload)
        self.assertEqual(4, len(events))
        adds = {x.ticker: x for x in events if x.event_type == "ADD"}
        removes = {x.ticker: x for x in events if x.event_type == "REMOVE"}
        self.assertEqual({"OTIS", "CARR"}, set(adds))
        self.assertEqual({"RTN", "M"}, set(removes))
        self.assertTrue(all(x.effective_date.isoformat() == "2020-04-03" for x in adds.values()))
        self.assertTrue(all(x.effective_date.isoformat() == "2020-04-06" for x in removes.values()))
        self.assertEqual("Otis Worldwide Corp.", adds["OTIS"].company_name)
        self.assertEqual("Macy's Inc.", removes["M"].company_name)

    def test_parser_accepts_move_variant_without_respectively_clause(self) -> None:
        payload = _narrative_announcement(
            "The changes will be effective prior to the open of trading on Monday, "
            "June 22. S&amp;P MidCap 400 constituents Tyler Technologies Inc. "
            "(NYSE:TYL), Bio-Rad Laboratories Inc. (NYSE:BIO) and Teledyne "
            "Technologies Inc. (NYSE:TDY) will move to the S&amp;P 500, replacing "
            "Harley-Davidson Inc. (NYSE:HOG), Nordstrom Inc. (NYSE:JWN) and "
            "Alliance Data Systems Corp. (NYSE:ADS) all of which will move to the "
            "S&amp;P MidCap 400.",
            itemdate="2020-06-12 18:18:00 EDT",
        )
        events = parse_sp500_membership_announcement(payload)
        self.assertEqual(6, len(events))
        self.assertEqual({"TYL", "BIO", "TDY"}, {x.ticker for x in events if x.event_type == "ADD"})
        self.assertEqual({"HOG", "JWN", "ADS"}, {x.ticker for x in events if x.event_type == "REMOVE"})

    def test_parser_does_not_promote_a_narrative_title_without_action_terms(self) -> None:
        payload = _narrative_announcement(
            "Paycom Software Set to Join S&amp;P 500. This page contains no explicit "
            "replacement security or effective date."
        )
        self.assertEqual((), parse_sp500_membership_announcement(payload))

    def test_reparse_derives_a_new_batch_without_mutating_the_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = USPITStore(Path(directory))
            payload = _narrative_announcement(
                "Changes are effective prior to the opening on Tuesday, May 12: "
                "DexCom Inc. (NASD:DXCM) will replace Allergan plc (NYSE:AGN) "
                "in the S&amp;P 500."
            )
            reference = store.put_bytes(payload, media_type="text/html")
            from research_platform.us_pit.models import LicenseClass, SourceDependency, SourceRole

            capture = store.write_source_batch(
                [
                    SourceDependency(
                        source_id="spglobal_sp500_membership_events",
                        source_version="spglobal-press-sp500-raw-v1",
                        role=SourceRole.VALIDATION_ANCHOR,
                        license_class=LicenseClass.OFFICIAL_PUBLIC,
                        object_sha256=reference.sha256,
                        observed_at=OBSERVED.isoformat(),
                        url="https://press.spglobal.com/example",
                        dataset="membership_event_probe",
                        published_at="2020-05-06T19:23:00-04:00",
                        metadata={
                            "artifact_kind": "raw_spglobal_sp500_candidate_announcement",
                            "raw_frozen": True,
                            "response_sha256": reference.sha256,
                        },
                    )
                ]
            )
            derived = reparse_spglobal_event_probes(
                store,
                [capture.batch_id],
                start_date=date(2019, 10, 1),
                end_date=date(2026, 7, 31),
            )
            self.assertNotEqual(capture.batch_id, derived.batch_id)
            self.assertEqual("membership_event_probe", capture.dependencies[0].dataset)
            self.assertEqual("membership_events", derived.dependencies[0].dataset)
            self.assertEqual(reference.sha256, derived.dependencies[0].object_sha256)
            self.assertEqual(2, derived.dependencies[0].metadata["event_count"])

    def test_parser_uses_only_an_explicit_same_payload_holiday_override(self) -> None:
        payload = _announcement().replace(
            b"<table>",
            (
                b"<p>The changes are effective prior to the open of trading on "
                b"Tuesday, June 20, 2022. The U.S. equity markets will be closed on "
                b"Monday, June 19, 2022 in observance of the holiday.</p><table>"
            ),
        ).replace(b"Dec. 20, 2021", b"June 19, 2022")
        events = parse_sp500_membership_announcement(payload)
        self.assertEqual({date(2022, 6, 20)}, {item.effective_date for item in events})

        no_explicit_closure = _announcement().replace(
            b"Dec. 20, 2021", b"June 19, 2022"
        )
        events = parse_sp500_membership_announcement(no_explicit_closure)
        self.assertEqual({date(2022, 6, 19)}, {item.effective_date for item in events})

    def test_adapter_freezes_archive_and_announcement_without_backdating(self) -> None:
        link = "https://press.spglobal.com/2021-12-03-Apple-Set-to-Join-S-P-500"
        transport = _Transport(link)
        adapter = SPGlobalSP500MembershipEventAdapter(
            transport=transport,
            clock=lambda: OBSERVED,
            minimum_request_interval_seconds=0,
        )
        artifacts = tuple(
            adapter.fetch(
                SyncRequest(date(2021, 1, 1), date(2021, 12, 31), OBSERVED)
            )
        )
        events = [item for item in artifacts if item.dataset == "membership_events"]
        self.assertEqual(1, len(events))
        self.assertEqual(2, events[0].metadata["event_count"])
        self.assertEqual("SIGNAL_INPUT", events[0].role.value)
        self.assertEqual("2021-12-03T19:52:00-05:00", events[0].published_at.isoformat())
        self.assertTrue(any(item.dataset == "membership_event_index" for item in artifacts))
        self.assertTrue(any(item.dataset == "membership_event_probe" for item in artifacts))

    def test_adapter_retries_transient_read_only_capture_without_partial_batch(self) -> None:
        link = "https://press.spglobal.com/2021-12-03-Apple-Set-to-Join-S-P-500"
        transport = _RetryTransport(link)
        delays: list[float] = []
        adapter = SPGlobalSP500MembershipEventAdapter(
            transport=transport,
            clock=lambda: OBSERVED,
            minimum_request_interval_seconds=0,
            retry_backoff_seconds=0.25,
            sleeper=delays.append,
        )
        artifacts = tuple(
            adapter.fetch(
                SyncRequest(date(2021, 1, 1), date(2021, 12, 31), OBSERVED)
            )
        )
        self.assertTrue(any(item.dataset == "membership_events" for item in artifacts))
        self.assertEqual([0.25], delays)

    def test_candidate_output_requires_review_and_binds_directional_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = USPITService(root / "pit")
            store = service.store
            link = "https://press.spglobal.com/2021-12-03-Apple-Set-to-Join-S-P-500"
            batch = service.sync(
                SPGlobalSP500MembershipEventAdapter(
                    transport=_Transport(link), clock=lambda: OBSERVED,
                    minimum_request_interval_seconds=0,
                ),
                SyncRequest(date(2021, 1, 1), date(2021, 12, 31), OBSERVED),
            )
            normalization = root / "norm"
            normalization.mkdir()
            aapl_source = store.put_bytes(b"official-aapl-identity")
            old_source = store.put_bytes(b"official-old-identity")
            identity_frame = pd.DataFrame(
                [
                    {
                        "source_id": "ishares_ivv_holdings_api",
                        "ticker": "AAPL",
                        "share_class": "",
                        "as_of_date": "2021-12-31",
                        "identity_candidate_key": "isin:US0378331005",
                        "isin": "US0378331005",
                        "cusip": "037833100",
                        "content_sha256": aapl_source.sha256,
                        "source_row_number": 1,
                    },
                    {
                        "source_id": "ishares_ivv_holdings_api",
                        "ticker": "OLD",
                        "share_class": "",
                        "as_of_date": "2021-11-30",
                        "identity_candidate_key": "isin:US0000000001",
                        "isin": "US0000000001",
                        "cusip": "000000000",
                        "content_sha256": old_source.sha256,
                        "source_row_number": 2,
                    },
                ]
            )
            identity_path = normalization / "security_identity_candidates.parquet"
            identity_frame.to_parquet(identity_path, index=False)
            (normalization / "manifest.json").write_bytes(
                canonical_json_bytes(
                    {
                        "normalization_id": "norm",
                        "artifacts": {
                            "security_identity_candidates": {
                                "object_sha256": sha256_file(identity_path)
                            }
                        },
                    }
                )
            )

            result = build_spglobal_event_candidates(
                store, [batch.batch_id], normalization, root / "events"
            )
            frame = pd.read_parquet(result.path / "membership_event_candidates.parquet")
            self.assertEqual(2, result.manifest["matched"])
            self.assertTrue(frame["status"].eq("REVIEW_REQUIRED").all())
            self.assertFalse(frame["approved"].any())
            self.assertEqual(
                result.manifest["artifact_sha256"],
                sha256_file(result.path / "membership_event_candidates.parquet"),
            )
            review = prepare_spglobal_event_review(result.path, root / "review")
            reviewed = pd.read_parquet(review.path / "membership_events.parquet")
            self.assertFalse(reviewed["approved"].any())
            self.assertTrue(reviewed["review_note"].eq("").all())
            self.assertEqual("REVIEW_REQUIRED", review.manifest["status"])
            self.assertFalse(review.manifest["direct_build_allowed"])
            self.assertTrue(result.manifest["policy"]["archive_pagination_replayed"])
            direct = review_spglobal_event_evidence(
                store,
                [batch.batch_id],
                result.path,
                normalization,
                root / "direct-review",
                reviewed_at=OBSERVED,
            )
            approved = pd.read_parquet(direct.path / "membership_events.parquet")
            self.assertEqual(2, direct.manifest["approved_rows"])
            self.assertEqual(0, direct.manifest["blocked_rows"])
            self.assertTrue(approved["approved"].all())
            self.assertEqual("REVIEWED", direct.manifest["status"])
            self.assertTrue(direct.manifest["direct_build_allowed"])

    def test_sec_name_identity_is_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = USPITService(root / "pit")
            first = "https://press.spglobal.com/2021-12-03-First-S-P-500"
            dependencies = []
            for link in (first,):
                batch = service.sync(
                    SPGlobalSP500MembershipEventAdapter(
                        transport=_Transport(link), clock=lambda: OBSERVED,
                        minimum_request_interval_seconds=0,
                    ),
                    SyncRequest(date(2021, 1, 1), date(2021, 12, 31), OBSERVED),
                )
                dependencies.append(batch.batch_id)
            normalization = root / "norm"
            normalization.mkdir()
            sec_source = service.store.put_bytes(b"sec-apple-identity")
            late_ishares_source = service.store.put_bytes(
                b"late-observed-ishares-apple-ticker"
            )
            identity_frame = pd.DataFrame(
                [
                    {
                        "source_id": "sec_nport_ivv",
                        "ticker": None,
                        "share_class": "Common Stock",
                        "as_of_date": "2021-12-31",
                        "identity_candidate_key": "isin:US0378331005",
                        "isin": "US0378331005",
                        "cusip": "037833100",
                        "content_sha256": sec_source.sha256,
                        "source_row_number": 1,
                        "issuer_name": "Apple Inc",
                        "title": "Apple Inc",
                    },
                    {
                        "source_id": "ishares_ivv_holdings_api",
                        "ticker": "AAPL",
                        "share_class": "Common Stock",
                        "as_of_date": "2026-03-31",
                        "identity_candidate_key": "isin:US0378331005",
                        "isin": "US0378331005",
                        "cusip": "037833100",
                        "content_sha256": late_ishares_source.sha256,
                        "source_row_number": 2,
                        "issuer_name": "Apple Inc",
                        "title": "Apple Inc",
                    },
                ]
            )
            identity_path = normalization / "security_identity_candidates.parquet"
            identity_frame.to_parquet(identity_path, index=False)
            (normalization / "manifest.json").write_bytes(
                canonical_json_bytes(
                    {
                        "normalization_id": "norm",
                        "artifacts": {
                            "security_identity_candidates": {
                                "object_sha256": sha256_file(identity_path)
                            }
                        },
                    }
                )
            )
            result = build_spglobal_event_candidates(
                service.store, dependencies, normalization, root / "events"
            )
            frame = pd.read_parquet(result.path / "membership_event_candidates.parquet")
            apple = frame.loc[frame["ticker_at_announcement"].eq("AAPL")]
            self.assertEqual(1, len(apple))
            self.assertEqual(
                "EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR",
                apple.iloc[0]["identity_match_basis"],
            )
            self.assertFalse(bool(apple.iloc[0]["approved"]))
            direct = review_spglobal_event_evidence(
                service.store,
                dependencies,
                result.path,
                normalization,
                root / "direct-review",
                reviewed_at=OBSERVED,
            )
            approved = pd.read_parquet(direct.path / "membership_events.parquet")
            self.assertEqual(1, direct.manifest["approved_rows"])
            self.assertEqual("AAPL", approved.iloc[0]["ticker_at_announcement"])
            self.assertIn(
                "independent SEC identifier and iShares ticker cross-evidence",
                approved.iloc[0]["review_note"],
            )

    def test_cli_contracts_are_explicit(self) -> None:
        sync = build_parser().parse_args(
            ["us-pit", "sync-sp500-events", "--start", "2021-08-01", "--end", "2026-07-31"]
        )
        self.assertEqual("sync-sp500-events", sync.us_pit_command)
        proposal = build_parser().parse_args(
            [
                "us-pit", "propose-membership-events", "--source-batch", "a" * 64,
                "--normalization-dir", "norm", "--output-dir", "out",
            ]
        )
        self.assertEqual("propose-membership-events", proposal.us_pit_command)
        review = build_parser().parse_args(
            [
                "us-pit", "prepare-membership-review",
                "--candidate-dir", "candidates", "--output-dir", "review",
            ]
        )
        self.assertEqual("prepare-membership-review", review.us_pit_command)
        direct_review = build_parser().parse_args(
            [
                "us-pit",
                "review-membership-events",
                "--source-batch",
                "a" * 64,
                "--candidate-dir",
                "candidates",
                "--normalization-dir",
                "norm",
                "--output-dir",
                "review",
            ]
        )
        self.assertEqual("review-membership-events", direct_review.us_pit_command)


if __name__ == "__main__":
    unittest.main()
