from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from research_platform.us_pit.forward_capture import (
    USPITForwardCaptureService,
    forward_capture_task_spec,
    forward_capture_task_status,
    install_forward_capture_task,
    remove_forward_capture_task,
)
from research_platform.us_pit.models import LicenseClass, SourceRole
from research_platform.us_pit.service import USPITService
from research_platform.us_pit.sources import SourceAdapter, SourceArtifact, SyncRequest


ISHARES_CSV = b"""iShares Core S&P 500 ETF\nFund Holdings as of,\"Aug 13, 2026\"\n\nTicker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\nAAPL,APPLE INC,Information Technology,Equity,100,100,100,1,100,United States,NASDAQ,USD,1,USD,-\n"""


def _tdx_payload() -> bytes:
    response = {
        "result": {
            "ErrorId": "0",
            "Value": [{"Code": "AAPL.US", "Name": "Apple"}],
        }
    }
    return json.dumps(
        {
            "method": "get_stock_list",
            "request_utf8": json.dumps(
                {"method": "get_stock_list", "params": {"market": "103"}}
            ),
            "response_utf8": json.dumps(response),
            "fetched_at": "2026-08-13T20:20:00+00:00",
        }
    ).encode()


class _Adapter(SourceAdapter):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0
        if kind == "ishares":
            self.source_id = "ishares_ivv_holdings"
            self.source_version = "ishares-ivv-observed-raw-v1"
        else:
            self.source_id = "tdx_us_security_master_current"
            self.source_version = "tq-market-103-raw-v1"

    def fetch(self, request: SyncRequest):
        self.calls += 1
        if self.kind == "ishares":
            return (
                SourceArtifact(
                    dataset="fund_holdings_observed",
                    payload=ISHARES_CSV,
                    media_type="text/csv",
                    url="https://www.ishares.com/ivv.csv",
                    observed_at=request.observed_at,
                    published_at=request.observed_at,
                    as_of_date=request.observed_at.date(),
                    role=SourceRole.SIGNAL_INPUT,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "artifact_kind": "raw_observed_holdings_csv",
                        "observation_mode": "current",
                        "eligible_for_historical_signal": True,
                        "eligible_from": request.observed_at.isoformat(),
                        "response_sha256": "placeholder",
                    },
                ),
            )
        return (
            SourceArtifact(
                dataset="us_security_master_current",
                payload=_tdx_payload(),
                media_type="application/json",
                url="http://127.0.0.1:17709/get_stock_list",
                observed_at=request.observed_at,
                published_at=request.observed_at,
                as_of_date=request.observed_at.date(),
                role=SourceRole.CROSS_CHECK,
                license_class=LicenseClass.LOCAL_VENDOR,
                metadata={"membership_authority": False},
            ),
        )


class ForwardCaptureTests(unittest.TestCase):
    def test_market_close_and_pre_close_are_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = USPITForwardCaptureService(Path(directory))
            holiday = capture.capture(
                observed_at=datetime(2026, 7, 4, 20, tzinfo=timezone.utc)
            )
            preclose = capture.capture(
                observed_at=datetime(2026, 8, 13, 19, tzinfo=timezone.utc)
            )
            self.assertEqual(holiday.status, "MARKET_CLOSED")
            self.assertEqual(preclose.status, "WAITING_FOR_POST_CLOSE")
            self.assertEqual(capture.status()["capture_count"], 0)

    def test_post_close_capture_is_append_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pit = USPITService(Path(directory))
            capture = USPITForwardCaptureService(pit)
            ishares = _Adapter("ishares")
            tdx = _Adapter("tdx")
            observed = datetime(2026, 8, 13, 20, 20, tzinfo=timezone.utc)

            # The official normalizer requires the stored object hash in source
            # metadata. Use a deterministic adapter that fills it before sync.
            original_fetch = ishares.fetch

            def fetch_with_hash(request: SyncRequest):
                artifact = original_fetch(request)[0]
                from research_platform.us_pit.hashing import sha256_bytes

                return (
                    SourceArtifact(
                        **{
                            **artifact.__dict__,
                            "metadata": {
                                **artifact.metadata,
                                "response_sha256": sha256_bytes(artifact.payload),
                            },
                        }
                    ),
                )

            ishares.fetch = fetch_with_hash  # type: ignore[method-assign]
            first = capture.capture(
                observed_at=observed,
                ishares_adapter=ishares,
                tdx_adapter=tdx,
            )
            second = capture.capture(
                observed_at=observed,
                ishares_adapter=ishares,
                tdx_adapter=tdx,
            )

            self.assertEqual(first.status, "CAPTURED")
            self.assertEqual(second.status, "ALREADY_CAPTURED")
            self.assertEqual(first.receipt["capture_id"], second.receipt["capture_id"])
            self.assertEqual(first.receipt["alias_verified_count"], 1)
            self.assertEqual(
                [first.receipt["ishares_source_batch_id"]],
                first.receipt["official_source_batch_ids"],
            )
            self.assertFalse(first.receipt["historical_membership_authority"])
            self.assertEqual(ishares.calls, 1)
            self.assertEqual(tdx.calls, 1)

            receipt_path = first.path / "receipt.json"
            value = json.loads(receipt_path.read_text(encoding="utf-8"))
            value["alias_verified_count"] = 2
            receipt_path.chmod(0o666)
            receipt_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                capture.status(now=observed)

    def test_task_scheduler_contract_is_read_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[list[str]] = []

            def runner(command, **kwargs):
                del kwargs
                calls.append(list(command))
                if command[1] == "/Query":
                    return subprocess.CompletedProcess(command, 1, "", "not found")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            spec = forward_capture_task_spec(
                python_executable="python.exe",
                project_root=directory,
            )
            self.assertEqual(
                spec["action"]["arguments"],
                "-m research_platform us-pit capture-current",
            )
            installed = install_forward_capture_task(spec, runner=runner)
            self.assertTrue(installed["registered"])
            self.assertFalse(installed["broker_writes_enabled"])
            status = forward_capture_task_status(runner=runner)
            self.assertFalse(status["registered"])
            removed = remove_forward_capture_task(runner=runner)
            self.assertFalse(removed["removed"])
            self.assertTrue(all("us-paper" not in " ".join(call) for call in calls))


if __name__ == "__main__":
    unittest.main()
