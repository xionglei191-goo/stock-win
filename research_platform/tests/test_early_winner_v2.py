from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research_platform.early_winner_v2_research import (
    EarlyWinnerV2ResearchService,
    _fit_preprocessor,
    _prepare_v2_labels,
    profile_development_data,
)
from research_platform.early_winner_research import ResearchDataBlockedError
from research_platform.storage import Database, _file_sha256
from research_platform.tests.helpers import temporary_config


def _row(asof: str, index: int, *, executable: bool = True) -> dict[str, object]:
    row: dict[str, object] = {
        "code": f"600{index:03d}.SH",
        "asof": asof,
        "listed_days": 300,
        "valid_days_20": 20,
        "adv20": 200_000_000.0,
        "suspended": False,
        "is_st": False,
        "is_quit": False,
        "entry_executable": executable,
        "forward_return_60": index / 100.0 if executable else np.nan,
        "published_at": f"{asof}T14:00:00",
        "effective_at": f"{asof}T14:00:00",
        "close": 10.0 + index,
        "ma60": 10.0,
        "industry": f"行业{index % 4}",
    }
    for column in (
        "industry_momentum", "industry_breadth", "industry_amount_trend",
        "revenue_yoy", "profit_yoy", "gross_margin_change", "roe",
        "ocf_profit_ratio", "forecast_revision", "return_20", "return_60",
        "return_120", "relative_return_20", "relative_return_60",
        "relative_return_120", "volume_ratio", "amount_ratio",
        "breakout_distance", "ma20_slope", "event_score",
        "northbound_change_ratio", "institution_lhb_ratio",
        "institution_holding_change_ratio", "shareholder_count_change",
        "turnover_20", "price_to_ma60", "valuation_percentile",
    ):
        row[column] = float(index + 1)
    return row


class EarlyWinnerV2Tests(unittest.TestCase):
    def test_label_scope_excludes_rows_without_execution_outcomes(self) -> None:
        rows = [_row("2020-06-05", index) for index in range(20)]
        rows.extend(_row("2020-06-05", index + 20, executable=False) for index in range(10))

        prepared = _prepare_v2_labels(pd.DataFrame(rows))

        self.assertEqual(int(prepared["v2_eligible"].sum()), 20)
        self.assertTrue(prepared.loc[~prepared["v2_eligible"], "target"].isna().all())
        self.assertEqual(int(prepared.loc[prepared["v2_eligible"], "target"].sum()), 1)

    def test_preprocessor_drops_empty_constant_features_and_uses_train_bounds(self) -> None:
        train = pd.DataFrame(
            {
                "signal": [0.0, 1.0, 2.0, 1_000_000.0],
                "constant": [0.5, 0.5, 0.5, 0.5],
                "empty": [np.nan, np.nan, np.nan, np.nan],
            }
        )
        transformed, specification = _fit_preprocessor(
            train, ("signal", "constant", "empty")
        )

        self.assertEqual(list(specification), ["signal"])
        self.assertEqual(list(transformed), ["signal"])
        self.assertLess(float(transformed["signal"].max()), 1_000_000.0)

    def test_profile_finds_v1_label_and_no_information_feature_failures(self) -> None:
        rows = [_row("2020-06-05", index) for index in range(20)]
        rows.extend(_row("2020-06-05", index + 20, executable=False) for index in range(10))
        frame = pd.DataFrame(rows)
        frame["turnover_20"] = np.nan
        frame["valuation_percentile"] = 0.5

        profile = profile_development_data(frame)
        finding_codes = {item["code"] for item in profile["findings"]}

        self.assertEqual(profile["grain"]["duplicate_keys"], 0)
        self.assertTrue(profile["time_audit"]["passed"])
        self.assertEqual(
            finding_codes,
            {"V1_LABEL_SCOPE", "NO_INFORMATION_FEATURES", "FLOW_SCALE_AND_DRIFT"},
        )

    def test_development_batches_never_include_v1_frozen_test_years(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            database.upsert_research_project(
                project_id="early_winner_v1",
                version="1.0.0",
                name="fixture-v1",
                description="fixture",
            )
            for year in range(2018, 2025):
                path = Path(directory) / f"{year}.parquet"
                pd.DataFrame([_row(f"{year}-06-05", 1)]).to_parquet(path, index=False)
                database.save_research_data_batch(
                    {
                        "batch_id": f"batch-{year}",
                        "project_id": "early_winner_v1",
                        "dataset": "early_winner_features",
                        "source": "fixture",
                        "status": "SUCCEEDED",
                        "fetched_at": f"{year}-12-31T15:00:00",
                        "published_start": f"{year}-01-01",
                        "published_end": f"{year}-12-31",
                        "row_count": 1,
                        "path": str(path),
                        "content_hash": _file_sha256(path),
                        "schema_hash": "schema",
                        "metadata": {},
                        "error": "",
                    }
                )
            service = EarlyWinnerV2ResearchService(config, database)

            batches = service._development_batches()

        self.assertEqual(
            [int(str(item["published_end"])[:4]) for item in batches],
            list(range(2018, 2024)),
        )

    def test_missing_development_year_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV2ResearchService(config, database)

            with self.assertRaises(ResearchDataBlockedError):
                service._development_batches()

    def test_strategy_is_research_only_and_emits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV2ResearchService(config, database)

            result = service.strategy.scan(asof="2026-08-12")

        self.assertEqual(result.signals, ())
        self.assertEqual(result.candidates, ())
        self.assertFalse(result.state["trade_signals_enabled"])
        self.assertFalse(result.state["forward_validation_opened"])


if __name__ == "__main__":
    unittest.main()
