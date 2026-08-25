from __future__ import annotations

import unittest

from research_platform.gp29_status_source_assessment import (
    BLOCK_AUDIT_START_ANCHOR,
    BLOCK_INTERVAL_GAP,
    BLOCK_STAR_SEMANTICS,
    EXPECTED_FULL_TARGET_COUNT,
    GP29StatusSourceAssessmentBlockedError,
    GP29StatusSourceCapabilityAssessment,
    MINIMUM_EXTERNAL_EVIDENCE,
    SOURCE_STATUS,
    build_frozen_gp29_source_capability_assessment,
    replay_frozen_gp29_source_capability_assessment,
)


class GP29StatusSourceAssessmentTests(unittest.TestCase):
    def test_assessment_is_structurally_unable_to_emit_quality_rows(self) -> None:
        artifact = build_frozen_gp29_source_capability_assessment()

        self.assertFalse(artifact.ready)
        self.assertEqual(artifact.quality_rows_emitted, 0)
        self.assertFalse(artifact.source_contract["quality_dataset_eligibility"])
        self.assertFalse(artifact.source_contract["gp29_rows_may_be_emitted"])
        self.assertFalse(artifact.source_contract["training_allowed"])
        self.assertFalse(artifact.source_contract["trading_allowed"])
        self.assertEqual(artifact.source_contract["status"], SOURCE_STATUS)
        self.assertNotIn("rows", artifact.to_dict())

    def test_coverage_reports_events_without_claiming_interval_coverage(self) -> None:
        coverage = build_frozen_gp29_source_capability_assessment().coverage

        self.assertEqual(coverage["sample_target_count"], 3)
        self.assertEqual(
            coverage["expected_full_target_count"], EXPECTED_FULL_TARGET_COUNT
        )
        self.assertEqual(coverage["sample_index_pagination_closed_count"], 3)
        self.assertEqual(coverage["sample_index_cold_replayed_count"], 1)
        self.assertEqual(coverage["sample_delisting_window_event_count"], 3)
        self.assertEqual(coverage["sample_audit_start_anchor_count"], 0)
        self.assertEqual(coverage["sample_full_interval_semantics_resolved_count"], 0)
        self.assertEqual(coverage["quality_rows_emitted"], 0)
        self.assertEqual(coverage["full_scope_security_count_admitted"], 0)

    def test_three_samples_freeze_official_event_dates_and_index_totals(self) -> None:
        artifact = build_frozen_gp29_source_capability_assessment()
        by_code = {item.code: item for item in artifact.observations}

        self.assertEqual((by_code["600432.SH"].index_page_count, by_code["600432.SH"].index_row_count), (11, 1092))
        self.assertEqual((by_code["000511.SZ"].index_page_count, by_code["000511.SZ"].index_row_count), (3, 79))
        self.assertEqual((by_code["688086.SH"].index_page_count, by_code["688086.SH"].index_row_count), (5, 492))

        main_start = by_code["600432.SH"].events[0]
        sz_start = by_code["000511.SZ"].events[0]
        star_start = by_code["688086.SH"].events[0]
        self.assertEqual((main_start.effective_date, main_start.through_date_inclusive, main_start.delisted_date), ("2018-05-30", "2018-07-11", "2018-07-13"))
        self.assertEqual((sz_start.effective_date, sz_start.through_date_inclusive, sz_start.delisted_date), ("2018-06-05", "2018-07-17", "2018-07-18"))
        self.assertEqual((star_start.effective_date, star_start.through_date_inclusive, star_start.delisted_date), ("2023-06-08", "2023-06-30", "2023-07-07"))

    def test_names_in_notices_are_not_backward_filled_as_audit_start_anchors(self) -> None:
        artifact = build_frozen_gp29_source_capability_assessment()

        for observation in artifact.observations:
            with self.subTest(code=observation.code):
                self.assertIsNone(observation.audit_start_anchor_sha256)
                self.assertIn(BLOCK_AUDIT_START_ANCHOR, observation.blockers)

        main = next(item for item in artifact.observations if item.code == "600432.SH")
        self.assertIn("*ST", main.events[0].title)
        self.assertIsNone(main.audit_start_anchor_sha256)

    def test_star_delisting_is_not_silently_equated_to_risk_warning_board(self) -> None:
        artifact = build_frozen_gp29_source_capability_assessment()
        star = next(item for item in artifact.observations if item.code == "688086.SH")

        semantic = next(
            item for item in star.events if item.event_type == "STAR_DELISTING_BOARD_SEMANTIC"
        )
        self.assertIn("do not enter the risk-warning board", semantic.statement)
        self.assertIn(BLOCK_STAR_SEMANTICS, star.blockers)
        self.assertIn(BLOCK_INTERVAL_GAP, star.blockers)

    def test_only_cninfo_sample_is_currently_bound_to_cold_replayed_manifest(self) -> None:
        artifact = build_frozen_gp29_source_capability_assessment()
        by_code = {item.code: item for item in artifact.observations}

        self.assertTrue(by_code["000511.SZ"].index_cold_replayed)
        self.assertEqual(
            by_code["000511.SZ"].index_manifest_sha256,
            "fb645ba1c60560ba31897f8c05f991e96c97656946b7c928bdf0d4152868d979",
        )
        self.assertFalse(by_code["600432.SH"].index_cold_replayed)
        self.assertFalse(by_code["688086.SH"].index_cold_replayed)

    def test_minimum_external_evidence_is_explicit_and_full_scope(self) -> None:
        artifact = build_frozen_gp29_source_capability_assessment()

        self.assertEqual(artifact.minimum_external_evidence, MINIMUM_EXTERNAL_EVIDENCE)
        self.assertIn(
            "OFFICIAL_EFFECTIVE_DATED_STATUS_AT_AUDIT_START_OR_LISTING_DATE_PER_SECURITY",
            artifact.minimum_external_evidence,
        )
        self.assertIn(
            "OFFICIAL_SSE_STAR_DELISTING_TO_GP29_SEMANTIC_RULING",
            artifact.minimum_external_evidence,
        )
        self.assertTrue(
            artifact.minimum_external_evidence[-1].endswith("ALL_239_SECURITIES")
        )

    def test_exact_replay_rejects_ready_or_evidence_rewrites(self) -> None:
        artifact = build_frozen_gp29_source_capability_assessment()
        value = artifact.to_dict()
        replayed = replay_frozen_gp29_source_capability_assessment(value)
        self.assertEqual(replayed.logical_content_sha256, artifact.logical_content_sha256)

        changed = dict(value)
        changed["ready"] = True
        with self.assertRaises(GP29StatusSourceAssessmentBlockedError):
            replay_frozen_gp29_source_capability_assessment(changed)

        changed = artifact.to_dict()
        changed["observations"][0]["events"][0]["effective_date"] = "2018-05-29"
        with self.assertRaises(GP29StatusSourceAssessmentBlockedError):
            replay_frozen_gp29_source_capability_assessment(changed)

    def test_artifact_cannot_be_caller_constructed(self) -> None:
        artifact = build_frozen_gp29_source_capability_assessment()
        with self.assertRaises(TypeError):
            GP29StatusSourceCapabilityAssessment(
                observations=artifact.observations,
                logical_content_sha256=artifact.logical_content_sha256,
                _seal=object(),
            )


if __name__ == "__main__":
    unittest.main()
