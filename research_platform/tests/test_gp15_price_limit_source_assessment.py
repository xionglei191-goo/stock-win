from __future__ import annotations

import unittest

from research_platform.gp15_price_limit_source_assessment import (
    BLOCK_NO_LIMIT_SCHEMA,
    EXPECTED_EXCHANGE_COUNTS,
    EXPECTED_FULL_TARGET_COUNT,
    GP15_SCHEMA,
    GP15PriceLimitSourceAssessmentBlockedError,
    GP15PriceLimitSourceCapabilityAssessment,
    MINIMUM_EXTERNAL_EVIDENCE,
    PROHIBITED_INFERENCES,
    SOURCE_STATUS,
    build_frozen_gp15_source_capability_assessment,
    replay_frozen_gp15_source_capability_assessment,
)


class GP15PriceLimitSourceAssessmentTests(unittest.TestCase):
    def test_assessment_is_structurally_unable_to_emit_quality_rows(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()

        self.assertFalse(artifact.ready)
        self.assertEqual(artifact.quality_rows_emitted, 0)
        self.assertFalse(artifact.training_allowed)
        self.assertFalse(artifact.trading_allowed)
        self.assertFalse(artifact.promotion_allowed)
        self.assertEqual(artifact.source_contract["status"], SOURCE_STATUS)
        self.assertFalse(artifact.source_contract["quality_dataset_eligibility"])
        self.assertFalse(artifact.source_contract["gp15_rows_may_be_emitted"])
        self.assertFalse(artifact.source_contract["training_allowed"])
        self.assertFalse(artifact.source_contract["trading_allowed"])
        self.assertFalse(artifact.source_contract["promotion_allowed"])
        self.assertFalse(artifact.to_dict()["training_allowed"])
        self.assertFalse(artifact.to_dict()["trading_allowed"])
        self.assertFalse(artifact.to_dict()["promotion_allowed"])
        self.assertNotIn("rows", artifact.to_dict())

    def test_gp15_schema_and_point_in_time_fields_are_explicit(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()

        self.assertEqual(tuple(artifact.source_contract["schema"]), GP15_SCHEMA)
        self.assertEqual(
            GP15_SCHEMA,
            (
                "exchange",
                "code",
                "trade_date",
                "limit_up",
                "limit_down",
                "published_at",
                "effective_at",
                "source_document_hash",
            ),
        )
        self.assertFalse(artifact.source_contract["current_day_ohlc_inference_allowed"])
        self.assertFalse(artifact.source_contract["post_event_backfill_allowed"])

    def test_frozen_coverage_reports_only_observed_gap(self) -> None:
        coverage = build_frozen_gp15_source_capability_assessment().coverage

        self.assertEqual(coverage["target_security_count"], EXPECTED_FULL_TARGET_COUNT)
        self.assertEqual(coverage["target_exchange_counts"], EXPECTED_EXCHANGE_COUNTS)
        self.assertEqual(coverage["observed_official_raw_security_count"], 56)
        self.assertEqual(coverage["observed_raw_session_count"], 46_394)
        self.assertEqual(coverage["observed_raw_session_gp15_gap_count"], 46_394)
        self.assertAlmostEqual(
            coverage["observed_security_coverage_ratio"], 56 / 239
        )
        self.assertIsNone(coverage["full_scope_required_session_count"])
        self.assertFalse(coverage["full_scope_daily_coverage_closed"])
        self.assertFalse(coverage["gp15_source_index_present"])
        self.assertEqual(coverage["quality_rows_emitted"], 0)
        self.assertEqual(coverage["full_scope_security_count_admitted"], 0)

    def test_daily_dependencies_include_every_required_rule_input(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()
        by_id = {item.requirement_id: item for item in artifact.input_requirements}

        self.assertEqual(
            set(by_id),
            {
                "OFFICIAL_SESSION_REFERENCE_PRICE",
                "OFFICIAL_CORPORATE_ACTION_REFERENCE_INPUTS",
                "VERSIONED_BOARD_DATE_RULE_REGIME",
                "POINT_IN_TIME_ST_STATUS",
                "IPO_RELISTING_NO_LIMIT_WINDOW",
                "DELISTING_PERIOD_SPECIAL_RULE",
                "PRICE_TICK_AND_ROUNDING_RULE",
                "ROW_LEVEL_POINT_IN_TIME_PROVENANCE",
            },
        )
        self.assertIn("previous close", by_id["OFFICIAL_SESSION_REFERENCE_PRICE"].required_value)
        self.assertIn("ex-right", by_id["OFFICIAL_SESSION_REFERENCE_PRICE"].required_value)
        self.assertIn("published_at", by_id["ROW_LEVEL_POINT_IN_TIME_PROVENANCE"].required_value)
        self.assertIn("effective_at", by_id["ROW_LEVEL_POINT_IN_TIME_PROVENANCE"].required_value)
        self.assertTrue(
            all(item.admitted_daily_row_count == 0 for item in by_id.values())
        )

    def test_current_day_ohlc_and_post_event_backfill_are_prohibited(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()

        self.assertEqual(artifact.prohibited_inferences, PROHIBITED_INFERENCES)
        self.assertIn(
            "CURRENT_SESSION_HIGH_OR_LOW_AS_LIMIT_UP_OR_LIMIT_DOWN",
            artifact.prohibited_inferences,
        )
        self.assertIn(
            "CURRENT_NAME_OR_POST_EVENT_NAME_BACKFILL_AS_HISTORICAL_ST_STATUS",
            artifact.prohibited_inferences,
        )
        self.assertIn(
            "TDX_GP15_OR_VENDOR_LOCK_STATE_AS_OFFICIAL_NUMERIC_PRICE_LIMITS",
            artifact.prohibited_inferences,
        )

    def test_official_samples_freeze_board_specific_delisting_rules(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()
        by_code = {
            item.code: item
            for item in artifact.rule_evidence
            if item.code is not None
        }

        self.assertIn(
            "600432_SH_DELISTING_PERIOD_LIMIT_RATIO_WAS_10_PERCENT_EXCEPT_SPECIAL_CASES",
            by_code["600432.SH"].observations,
        )
        self.assertIn(
            "000511_SZ_DELISTING_PERIOD_LIMIT_RATIO_WAS_10_PERCENT",
            by_code["000511.SZ"].observations,
        )
        self.assertIn(
            "688086_SH_DELISTING_FIRST_SESSION_2023_06_08_HAD_NO_PRICE_LIMIT",
            by_code["688086.SH"].observations,
        )
        self.assertIn(
            "688086_SH_OTHER_DELISTING_SESSIONS_USED_A_20_PERCENT_LIMIT",
            by_code["688086.SH"].observations,
        )

    def test_one_sample_rule_fact_is_cold_replayed_but_full_matrix_is_not(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()
        cold = [item for item in artifact.rule_evidence if item.raw_document_cold_replayed]

        self.assertEqual(len(cold), 1)
        self.assertEqual(cold[0].code, "000511.SZ")
        self.assertEqual(
            cold[0].source_document_sha256,
            "41241acdcad3417ab13022c6aad54757b0cc74f0254bc7a3c1b154a7d42fe0a1",
        )
        self.assertTrue(cold[0].point_in_time_fact_eligible)
        self.assertTrue(
            all(not item.complete_for_2018_2023_scope for item in artifact.rule_evidence)
        )
        self.assertEqual(artifact.coverage["complete_rule_evidence_count"], 0)

    def test_no_limit_sessions_are_an_explicit_contract_conflict(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()
        conflicts = {item.conflict_id: item for item in artifact.no_limit_schema_conflicts}

        self.assertEqual(
            set(conflicts),
            {"STAR_IPO_FIRST_FIVE_SESSIONS", "STAR_DELISTING_FIRST_SESSION"},
        )
        self.assertEqual(conflicts["STAR_DELISTING_FIRST_SESSION"].window, "2023-06-08")
        for conflict in conflicts.values():
            with self.subTest(conflict=conflict.conflict_id):
                self.assertEqual(conflict.code, "688086.SH")
                self.assertIn("positive numeric limit_up", conflict.current_contract_conflict)
                self.assertIn("NO_PRICE_LIMIT", conflict.required_resolution)
        self.assertIn(BLOCK_NO_LIMIT_SCHEMA, artifact.coverage["blockers"])

    def test_minimum_external_evidence_requires_hash_bound_full_scope(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()

        self.assertEqual(artifact.minimum_external_evidence, MINIMUM_EXTERNAL_EVIDENCE)
        self.assertIn(
            "CONTENT_ADDRESSED_DERIVATION_MANIFEST_BINDING_ALL_INPUT_HASHES_PER_DAILY_ROW",
            artifact.minimum_external_evidence,
        )
        self.assertIn(
            "EXPLICIT_NO_PRICE_LIMIT_REPRESENTATION_ACCEPTED_BY_THE_GP15_QUALITY_CONTRACT",
            artifact.minimum_external_evidence,
        )
        self.assertTrue(
            artifact.minimum_external_evidence[-1].endswith("ALL_239_SECURITIES")
        )

    def test_exact_replay_rejects_ready_or_evidence_rewrites(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()
        replayed = replay_frozen_gp15_source_capability_assessment(artifact.to_dict())
        self.assertEqual(replayed.logical_content_sha256, artifact.logical_content_sha256)

        changed = artifact.to_dict()
        changed["ready"] = True
        with self.assertRaises(GP15PriceLimitSourceAssessmentBlockedError):
            replay_frozen_gp15_source_capability_assessment(changed)

        changed = artifact.to_dict()
        changed["coverage"]["observed_raw_session_gp15_gap_count"] -= 1
        with self.assertRaises(GP15PriceLimitSourceAssessmentBlockedError):
            replay_frozen_gp15_source_capability_assessment(changed)

        changed = artifact.to_dict()
        changed["rule_evidence"][0]["complete_for_2018_2023_scope"] = True
        with self.assertRaises(GP15PriceLimitSourceAssessmentBlockedError):
            replay_frozen_gp15_source_capability_assessment(changed)

    def test_artifact_cannot_be_caller_constructed(self) -> None:
        artifact = build_frozen_gp15_source_capability_assessment()
        with self.assertRaises(TypeError):
            GP15PriceLimitSourceCapabilityAssessment(
                coverage_observation=artifact.coverage_observation,
                rule_evidence=artifact.rule_evidence,
                input_requirements=artifact.input_requirements,
                no_limit_schema_conflicts=artifact.no_limit_schema_conflicts,
                logical_content_sha256=artifact.logical_content_sha256,
                _seal=object(),
            )


if __name__ == "__main__":
    unittest.main()
