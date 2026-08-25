from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from research_platform.data import TdxProvider
from research_platform.service import PlatformService
from research_platform.tests.helpers import temporary_config


def _us_bars(multiplier: float = 1.0) -> pd.DataFrame:
    index = pd.bdate_range(end=pd.Timestamp("2026-07-31"), periods=320)
    close = np.linspace(50.0, 120.0 * multiplier, len(index))
    volume = np.full(len(index), 2_000_000.0)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": volume,
            "Amount": close * volume,
        },
        index=index,
    )


class _FakeUSProvider:
    universe: tuple[str, ...] = ("AAPL.US", "600000.SH")
    frames: dict[str, pd.DataFrame] = {
        "AAPL.US": _us_bars(1.1),
        "SPY.US": _us_bars(),
    }
    instances: list["_FakeUSProvider"] = []

    def __init__(self, *_: object, **__: object) -> None:
        self.requests: list[tuple[str, ...]] = []
        self.a_share_requests = 0
        type(self).instances.append(self)

    def __enter__(self) -> "_FakeUSProvider":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def list_us_stocks(self) -> tuple[list[str], dict[str, str]]:
        codes = list(type(self).universe)
        return codes, {code: code.removesuffix(".US") for code in codes}

    def list_a_shares(self) -> tuple[list[str], dict[str, str]]:
        self.a_share_requests += 1
        return ["600000.SH"], {"600000.SH": "CN only"}

    def fetch_bars(
        self,
        codes: list[str],
        _period: str,
        _count: int,
        *,
        fields: tuple[str, ...] = (
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "Amount",
        ),
        batch_callback: object | None = None,
        **_: object,
    ) -> dict[str, pd.DataFrame]:
        self.requests.append(tuple(codes))
        if callable(batch_callback):
            batch_callback(len(codes), len(codes), len(codes))
        result: dict[str, pd.DataFrame] = {}
        for code in codes:
            frame = type(self).frames.get(code)
            if frame is None:
                continue
            selected = [field for field in fields if field in frame.columns]
            result[code] = frame.loc[:, selected].copy()
        return result

    def effective_batch_sizes(self) -> dict[str, list[int]]:
        return {"bars": [len(item) for item in self.requests], "events": []}


class USMarketRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeUSProvider.universe = ("AAPL.US", "600000.SH")
        _FakeUSProvider.frames = {
            "AAPL.US": _us_bars(1.1),
            "SPY.US": _us_bars(),
        }
        _FakeUSProvider.instances = []

    def test_tdx_provider_proxies_and_caches_us_security_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = TdxProvider(
                temporary_config(Path(directory)),
                __file__,
            )
            provider.adapter.list_us_stocks = Mock(
                return_value=(["AAPL.US"], {"AAPL.US": "Apple"})
            )

            first = provider.list_us_stocks()
            second = provider.list_us_stocks()

        self.assertEqual(first, (["AAPL.US"], {"AAPL.US": "Apple"}))
        self.assertEqual(second, first)
        provider.adapter.list_us_stocks.assert_called_once_with()

    def test_us_current_constituent_scan_is_sealed_before_any_market_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "research_platform.service.TdxProvider",
            _FakeUSProvider,
        ):
            service = PlatformService(temporary_config(Path(directory)))
            self.assertEqual(service._strategy_market("course49_system"), "CN")
            self.assertEqual(service._strategy_market("us_momentum_v1"), "US")
            with self.assertRaisesRegex(ValueError, "cannot combine CN and US"):
                service._scan_market(("course49_system", "us_momentum_v1"))

            with self.assertRaisesRegex(ValueError, "not enabled for scanning"):
                service.run_scan(["us_momentum_v1"])

        self.assertEqual(_FakeUSProvider.instances, [])

    def test_us_scan_cannot_be_reenabled_by_changing_current_universe(self) -> None:
        _FakeUSProvider.universe = ("600000.SH",)
        with tempfile.TemporaryDirectory() as directory, patch(
            "research_platform.service.TdxProvider",
            _FakeUSProvider,
        ):
            with self.assertRaisesRegex(ValueError, "not enabled for scanning"):
                PlatformService(temporary_config(Path(directory))).run_scan(
                    ["us_momentum_v1"]
                )
        self.assertEqual(_FakeUSProvider.instances, [])


if __name__ == "__main__":
    unittest.main()
