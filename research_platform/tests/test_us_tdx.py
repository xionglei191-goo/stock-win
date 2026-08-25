from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research_platform.us_tdx import TQReadOnlyClient, _health_rpc, check_tq_preflight


class USPaperTQTests(unittest.TestCase):
    def test_preflight_stops_in_required_order(self) -> None:
        missing = check_tq_preflight(
            system_name="Windows",
            installation_finder=lambda: [],
            process_finder=lambda: self.fail("process check must not run"),
            rpc_probe=lambda: self.fail("RPC check must not run"),
        )
        self.assertFalse(missing.ready)
        self.assertEqual([item.name for item in missing.checks], ["WINDOWS", "TQ_INSTALLED"])

        stopped = check_tq_preflight(
            system_name="Windows",
            installation_finder=lambda: [{"display_name": "TDX", "install_location": "D:/TDX"}],
            process_finder=lambda: False,
            rpc_probe=lambda: self.fail("RPC check must not run"),
        )
        self.assertEqual(stopped.checks[-1].name, "TDXW_RUNNING")

    def test_preflight_requires_successful_json_rpc(self) -> None:
        ready = check_tq_preflight(
            system_name="Windows",
            installation_finder=lambda: [{"display_name": "TDX", "install_location": "D:/TDX"}],
            process_finder=lambda: True,
            rpc_probe=lambda: {"result": {"ErrorId": "0", "Value": []}},
        )
        self.assertTrue(ready.ready)
        self.assertEqual(ready.as_dict()["status"], "READY")

    def test_project_confirmed_portable_runtime_does_not_require_registry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TdxW.exe").touch()
            with patch(
                "research_platform.us_tdx._registered_installations",
                return_value=[],
            ):
                ready = check_tq_preflight(
                    system_name="Windows",
                    portable_root=root,
                    process_finder=lambda: True,
                    rpc_probe=lambda: {
                        "result": {"ErrorId": "0", "Value": []}
                    },
                )

        self.assertTrue(ready.ready)
        installed = next(
            item for item in ready.checks if item.name == "TQ_INSTALLED"
        )
        self.assertIn("portable TQ runtime", installed.detail)
        self.assertIn(str(root), installed.detail)

    def test_client_is_structurally_read_only_and_requires_source_time(self) -> None:
        client = TQReadOnlyClient()
        with self.assertRaises(PermissionError):
            client.call("order_stock", {"account_id": 0})

        client.call = lambda method, params: {  # type: ignore[method-assign]
            "Open": "100",
            "Now": "101",
            "Buyp": ["100.9"],
            "Sellp": ["101.1"],
        }
        observed = client.market_snapshot(
            "AAPL.US",
            fetched_at=datetime(2026, 8, 12, 9, 31, tzinfo=ZoneInfo("America/New_York")),
        )
        self.assertIsNone(observed.source_at)
        self.assertEqual(observed.bid, 100.9)
        self.assertEqual(observed.ask, 101.1)

    def test_health_probe_requires_real_us_list_and_daily_data(self) -> None:
        class FakeClient:
            def __init__(self):
                self.calls = []

            def call(self, method, params):
                self.calls.append((method, params))
                if method == "get_stock_list":
                    return {"Value": [{"Code": "AAPL.US"}]}
                if method == "get_market_data":
                    return {"AAPL.US": {"Close": [1.0, 1.1]}}
                return {"ok": True}

        fake = FakeClient()
        with patch("research_platform.us_tdx.TQReadOnlyClient", return_value=fake):
            response = _health_rpc()
        self.assertEqual(response["result"]["ErrorId"], "0")
        self.assertEqual(
            [item[0] for item in fake.calls],
            ["get_stock_list", "get_market_data", "get_match_stkinfo"],
        )

    def test_default_preflight_exposes_us_read_only_market_gate(self) -> None:
        with patch(
            "research_platform.us_tdx._health_rpc",
            return_value={
                "result": {
                    "ErrorId": "0",
                    "Value": {
                        "us_stock_list_count": 13926,
                        "us_market_data_probe": "AAPL.US",
                    },
                }
            },
        ):
            ready = check_tq_preflight(
                system_name="Windows",
                installation_finder=lambda: [
                    {"display_name": "TDX", "install_location": "D:/TDX"}
                ],
                process_finder=lambda: True,
            )
        self.assertTrue(ready.ready)
        self.assertEqual(ready.checks[-1].name, "TQ_US_READ_ONLY_MARKET_DATA")
        self.assertIn("13926", ready.checks[-1].detail)

    def test_explicit_portable_runtime_never_enumerates_registry(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TdxW.exe").write_bytes(b"fixture")
            with patch(
                "research_platform.us_tdx._registered_installations",
                side_effect=AssertionError("registry must not be queried"),
            ):
                result = check_tq_preflight(
                    system_name="Windows",
                    portable_root=root,
                    process_finder=lambda: False,
                )
            self.assertFalse(result.ready)
            self.assertIn(str(root), result.checks[1].detail)
            self.assertIn("portable", result.checks[1].detail.casefold())


if __name__ == "__main__":
    unittest.main()
