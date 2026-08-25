from __future__ import annotations

import unittest

from research_platform.adjusted_bar_factor_source_assessment import (
    ADJUSTED_BAR_FACTOR_SCHEMA,
    BLOCK_2017_ANCHOR,
    BLOCK_DUAL_SOURCE,
    BLOCK_FACTOR_CHANGES,
    BLOCK_FACTOR_SOURCE,
    BLOCK_GP30,
    BLOCK_GP43,
    BLOCK_RAW_SCOPE,
    EXPECTED_EXCHANGE_COUNTS,
    EXPECTED_FULL_TARGET_COUNT,
    MINIMUM_AUTHORIZED_SOURCES,
    MINIMUM_EXTERNAL_EVIDENCE,
    PROHIBITED_INFERENCES,
    SOURCE_STATUS,
    AdjustedBarFactorSourceAssessmentBlockedError,
    AdjustedBarFactorSourceCapabilityAssessment,
    build_frozen_adjusted_bar_factor_source_capability_assessment,
    replay_frozen_adjusted_bar_factor_source_capability_assessment,
)


class AdjustedBarFactorSourceAssessmentTests(unittest.TestCase):
    def test_assessment_is_structurally_unable_to_emit_or_promote_rows(self) -> None:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
        value = artifact.to_dict()

        self.assertFalse(artifact.ready)
        self.assertEqual(artifact.quality_rows_emitted, 0)
        self.assertEqual(artifact.quality_row_count, 0)
        self.assertFalse(artifact.training_allowed)
        self.assertFalse(artifact.trading_allowed)
        self.assertFalse(artifact.promotion_allowed)
        self.assertEqual(artifact.source_contract["status"], SOURCE_STATUS)
        self.assertFalse(artifact.source_contract["quality_dataset_eligibility"])
        self.assertFalse(
            artifact.source_contract["adjusted_bar_factor_rows_may_be_emitted"]
        )
        self.assertFalse(value["ready"])
        self.assertEqual(value["quality_rows_emitted"], 0)
        self.assertEqual(value["quality_row_count"], 0)
        self.assertFalse(value["training_allowed"])
        self.assertFalse(value["trading_allowed"])
        self.assertFalse(value["promotion_allowed"])
        self.assertNotIn("rows", value)

    def test_frozen_239_coverage_quantifies_sse_and_szse_gaps(self) -> None:
        coverage = build_frozen_adjusted_bar_factor_source_capability_assessment().coverage

        self.assertEqual(coverage["target_security_count"], EXPECTED_FULL_TARGET_COUNT)
        self.assertEqual(coverage["target_exchange_counts"], EXPECTED_EXCHANGE_COUNTS)
        self.assertEqual(coverage["by_exchange"]["SSE"]["official_raw_security_count"], 56)
        self.assertEqual(coverage["by_exchange"]["SSE"]["raw_security_missing_count"], 43)
        self.assertTrue(coverage["by_exchange"]["SSE"]["raw_source_index_present"])
        self.assertEqual(coverage["by_exchange"]["SZSE"]["official_raw_security_count"], 0)
        self.assertEqual(coverage["by_exchange"]["SZSE"]["raw_security_missing_count"], 140)
        self.assertFalse(coverage["by_exchange"]["SZSE"]["raw_source_index_present"])
        self.assertEqual(coverage["official_raw_security_count"], 56)
        self.assertEqual(coverage["raw_security_missing_count"], 183)
        self.assertAlmostEqual(coverage["raw_security_coverage_ratio"], 56 / 239)
        self.assertEqual(coverage["admitted_factor_security_count"], 0)
        self.assertEqual(coverage["admitted_factor_row_count"], 0)
        self.assertEqual(coverage["admitted_2017_anchor_count"], 0)
        self.assertEqual(coverage["full_scope_security_count_admitted"], 0)
        self.assertFalse(coverage["full_scope_daily_coverage_closed"])
        self.assertIn(BLOCK_RAW_SCOPE, coverage["blockers"])
        self.assertIn(BLOCK_FACTOR_SOURCE, coverage["blockers"])

    def test_schema_and_per_bar_arithmetic_contract_are_exact(self) -> None:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
        arithmetic = artifact.arithmetic_contract

        self.assertEqual(tuple(artifact.source_contract["schema"]), ADJUSTED_BAR_FACTOR_SCHEMA)
        self.assertEqual(
            arithmetic["equations"],
            [
                "front_open=raw_open*adjustment_factor",
                "front_high=raw_high*adjustment_factor",
                "front_low=raw_low*adjustment_factor",
                "front_close=raw_close*adjustment_factor",
            ],
        )
        self.assertEqual(arithmetic["relative_tolerance"], 1e-9)
        self.assertEqual(arithmetic["absolute_tolerance"], 1e-6)
        self.assertFalse(arithmetic["raw_bar_without_adjusted_row_allowed"])
        self.assertFalse(arithmetic["adjusted_row_without_raw_bar_allowed"])

    def test_2017_anchor_and_factor_change_event_closure_are_hard_gates(self) -> None:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
        contract = artifact.factor_change_contract
        coverage = artifact.coverage

        self.assertEqual(contract["first_comparison_value"], "anchor_adjustment_factor")
        self.assertTrue(contract["anchor_trade_date_must_precede_first_partition_trade_date"])
        self.assertTrue(contract["anchor_fields_only_on_first_partition_row"])
        self.assertTrue(contract["2017_anchor_required_for_security_active_at_2018_boundary"])
        self.assertEqual(contract["relative_tolerance"], 1e-12)
        self.assertEqual(contract["absolute_tolerance"], 1e-12)
        self.assertTrue(contract["gp30_gp43_sources_must_be_independent"])
        self.assertTrue(contract["factor_change_dates_must_equal_reconciled_event_ex_dates"])
        self.assertFalse(contract["factor_change_without_event_allowed"])
        self.assertFalse(contract["event_without_factor_change_allowed"])
        self.assertIn(BLOCK_2017_ANCHOR, coverage["blockers"])
        self.assertIn(BLOCK_FACTOR_CHANGES, coverage["blockers"])

    def test_formal_corporate_action_failures_stay_at_zero_quality_rows(self) -> None:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
        evidence = {
            item.evidence_id: item
            for item in artifact.corporate_action_failure_evidence
        }
        coverage = artifact.coverage

        self.assertEqual(set(evidence), {
            "SSE_CNINFO_ANNOUNCEMENT_DUAL_SOURCE_SAMPLE",
            "SSE_STRUCTURED_CASH_DIVIDEND_CORROBORATION",
        })
        self.assertEqual(
            evidence["SSE_CNINFO_ANNOUNCEMENT_DUAL_SOURCE_SAMPLE"].target_security_count,
            3,
        )
        self.assertEqual(
            evidence["SSE_STRUCTURED_CASH_DIVIDEND_CORROBORATION"].candidate_or_corroboration_row_count,
            2,
        )
        for item in evidence.values():
            with self.subTest(evidence=item.evidence_id):
                self.assertFalse(item.ready)
                self.assertEqual(item.gp30_quality_row_count, 0)
                self.assertEqual(item.gp43_quality_row_count, 0)
                self.assertEqual(item.factor_eligible_event_count, 0)
        self.assertEqual(coverage["gp30_quality_row_count"], 0)
        self.assertEqual(coverage["gp43_quality_row_count"], 0)
        self.assertEqual(coverage["reconciled_dual_source_event_count"], 0)
        self.assertIn(BLOCK_GP30, coverage["blockers"])
        self.assertIn(BLOCK_GP43, coverage["blockers"])
        self.assertIn(BLOCK_DUAL_SOURCE, coverage["blockers"])

    def test_tdx_gaps_price_jumps_and_single_announcements_are_prohibited(self) -> None:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()

        self.assertEqual(artifact.prohibited_inferences, PROHIBITED_INFERENCES)
        self.assertIn(
            "TDX_EMPTY_NULL_OR_UNAVAILABLE_FACTOR_AS_FACTOR_ONE_OR_ZERO",
            artifact.prohibited_inferences,
        )
        self.assertIn(
            "RAW_PRICE_GAP_OR_OHLC_DISCONTINUITY_AS_A_FACTOR_OR_FACTOR_CHANGE",
            artifact.prohibited_inferences,
        )
        self.assertIn(
            "SINGLE_ANNOUNCEMENT_OR_SINGLE_CORPORATE_ACTION_SOURCE_AS_A_FACTOR",
            artifact.prohibited_inferences,
        )
        self.assertIn(
            "COPYING_ONE_DOCUMENT_OR_UPSTREAM_RECORD_INTO_BOTH_GP30_AND_GP43",
            artifact.prohibited_inferences,
        )
        self.assertFalse(artifact.source_contract["tdx_empty_value_inference_allowed"])
        self.assertFalse(artifact.source_contract["price_discontinuity_inference_allowed"])
        self.assertFalse(
            artifact.source_contract["single_corporate_action_source_inference_allowed"]
        )

    def test_minimum_authorizations_are_four_independent_full_scope_feeds(self) -> None:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
        requirements = {
            item.source_id: item for item in artifact.authorized_source_requirements
        }

        self.assertEqual(artifact.minimum_authorized_sources, MINIMUM_AUTHORIZED_SOURCES)
        self.assertEqual(artifact.minimum_external_evidence, MINIMUM_EXTERNAL_EVIDENCE)
        self.assertEqual(set(requirements), {
            "SSE_DAILY_RAW_FRONT_FACTOR_ARCHIVE",
            "SZSE_DAILY_RAW_FRONT_FACTOR_ARCHIVE",
            "GP30_CORPORATE_ACTION_HISTORY",
            "GP43_CORPORATE_ACTION_HISTORY",
        })
        self.assertEqual(requirements["SSE_DAILY_RAW_FRONT_FACTOR_ARCHIVE"].target_security_count, 99)
        self.assertEqual(requirements["SZSE_DAILY_RAW_FRONT_FACTOR_ARCHIVE"].target_security_count, 140)
        self.assertEqual(requirements["GP30_CORPORATE_ACTION_HISTORY"].target_security_count, 239)
        self.assertEqual(requirements["GP43_CORPORATE_ACTION_HISTORY"].target_security_count, 239)
        self.assertTrue(all(item.current_authorized_security_count == 0 for item in requirements.values()))
        self.assertIn("must not reuse", requirements["GP30_CORPORATE_ACTION_HISTORY"].independence_requirement)
        self.assertIn("must not reuse", requirements["GP43_CORPORATE_ACTION_HISTORY"].independence_requirement)

    def test_exact_replay_rejects_ready_coverage_and_provenance_rewrites(self) -> None:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
        replayed = replay_frozen_adjusted_bar_factor_source_capability_assessment(
            artifact.to_dict()
        )
        self.assertEqual(replayed.logical_content_sha256, artifact.logical_content_sha256)

        changed = artifact.to_dict()
        changed["ready"] = True
        with self.assertRaises(AdjustedBarFactorSourceAssessmentBlockedError):
            replay_frozen_adjusted_bar_factor_source_capability_assessment(changed)

        changed = artifact.to_dict()
        changed["coverage"]["admitted_factor_security_count"] = 1
        with self.assertRaises(AdjustedBarFactorSourceAssessmentBlockedError):
            replay_frozen_adjusted_bar_factor_source_capability_assessment(changed)

        changed = artifact.to_dict()
        changed["corporate_action_failure_evidence"][0]["gp30_quality_row_count"] = 1
        with self.assertRaises(AdjustedBarFactorSourceAssessmentBlockedError):
            replay_frozen_adjusted_bar_factor_source_capability_assessment(changed)

    def test_artifact_cannot_be_caller_constructed(self) -> None:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
        with self.assertRaises(TypeError):
            AdjustedBarFactorSourceCapabilityAssessment(
                exchange_coverage=artifact.exchange_coverage,
                corporate_action_failure_evidence=artifact.corporate_action_failure_evidence,
                admission_requirements=artifact.admission_requirements,
                authorized_source_requirements=artifact.authorized_source_requirements,
                logical_content_sha256=artifact.logical_content_sha256,
                _seal=object(),
            )


if __name__ == "__main__":
    unittest.main()
