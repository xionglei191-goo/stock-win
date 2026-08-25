from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from research_platform import bse_current_delisting_events as bse_current
from research_platform import pending_listing_source as pending_listing
from research_platform import security_master_observation as observation


FIXED_NOW = datetime(
    2026,
    8,
    13,
    1,
    49,
    tzinfo=timezone(timedelta(hours=8)),
)
PENDING_MANIFEST_SHA256 = (
    "8878c2be2e26ca534364311a3c86717d15c176bfcf8a3deeabf9771e3b2e9765"
)
PENDING_LOGICAL_SHA256 = (
    "81c2f4252c0d49591309b0a6b03cb8036a92de72b2a075ef284222b18212ed90"
)
BSE_MANIFEST_SHA256 = (
    "9a405d4e2499615abaca659fe08ede6f101cef2c1bbdb3a73623488664cbd8dd"
)
BSE_LOGICAL_SHA256 = (
    "2f713be4941af59b2038728b4c0d6df9dd199840514f665fa10ca1bfe6edf728"
)


class SecurityMasterObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for path in (
            observation.DEFAULT_PENDING_CAS_ROOT
            / "sha256"
            / PENDING_MANIFEST_SHA256[:2]
            / PENDING_MANIFEST_SHA256,
            observation.DEFAULT_BSE_CURRENT_DELISTING_CAS_ROOT
            / "manifests"
            / f"{BSE_MANIFEST_SHA256}.json",
        ):
            if not path.is_file():
                raise unittest.SkipTest("ignored official current-evidence CAS is absent")

        cls.shared_temporary = tempfile.TemporaryDirectory()
        root = Path(cls.shared_temporary.name)
        cls.pending_root = root / "pending"
        cls.bse_root = root / "bse"
        shutil.copytree(observation.DEFAULT_PENDING_CAS_ROOT, cls.pending_root)
        shutil.copytree(
            observation.DEFAULT_BSE_CURRENT_DELISTING_CAS_ROOT, cls.bse_root
        )
        pending_store = pending_listing.PendingListingManifestStore(
            pending_listing.PendingListingRawCAS(cls.pending_root)
        )
        pending_artifact = pending_store.replay(PENDING_MANIFEST_SHA256)
        pending_base = FIXED_NOW - timedelta(minutes=2)
        pending_sources = tuple(
            replace(
                item,
                retrieved_at=(pending_base + timedelta(seconds=index)).isoformat(),
            )
            for index, item in enumerate(pending_artifact.raw_sources)
        )
        pending_artifact = replace(
            pending_artifact,
            retrieved_at=(
                pending_base + timedelta(seconds=len(pending_sources) - 1)
            ).isoformat(),
            raw_sources=pending_sources,
        )
        pending_reference = pending_store.seal(pending_artifact)
        cls.pending_artifact = pending_store.replay(
            pending_reference.manifest_sha256
        )

        bse_store = bse_current.BSECurrentDelistingManifestStore(
            bse_current.BSECurrentDelistingCAS(cls.bse_root)
        )
        bse_artifact = bse_store.replay(BSE_MANIFEST_SHA256)
        bse_base = FIXED_NOW - timedelta(minutes=1)
        capture_index = 0
        notices = []
        for notice in bse_artifact.notices:
            attempts = []
            for attempt in notice.transport_attempts:
                retrieved_at = (
                    bse_base + timedelta(seconds=capture_index)
                ).isoformat()
                attempts.append(
                    replace(attempt, retrieved_at=retrieved_at)
                )
                capture_index += 1
            notices.append(replace(notice, transport_attempts=tuple(attempts)))
        pages = []
        for page in bse_artifact.catalogue_pages:
            pages.append(
                replace(
                    page,
                    retrieved_at=(
                        bse_base + timedelta(seconds=capture_index)
                    ).isoformat(),
                )
            )
            capture_index += 1
        closure = replace(
            bse_artifact.catalogue_closure_probe,
            retrieved_at=(bse_base + timedelta(seconds=capture_index)).isoformat(),
        )
        parsed_pages = tuple(
            bse_current._catalogue_evidence_from_dict(page.to_dict(), cas=bse_store.cas)
            for page in pages
        )
        parsed_closure = bse_current._catalogue_evidence_from_dict(
            closure.to_dict(), cas=bse_store.cas
        )
        bse_artifact = bse_current._build_artifact(
            notices=tuple(notices),
            pages=parsed_pages,
            closure=parsed_closure,
        )
        bse_reference = bse_store.seal(bse_artifact)
        cls.bse_artifact = bse_store.replay(bse_reference.manifest_sha256)

        cls.policy = observation.SecurityMasterObservationPolicy(
            pending_cas_root=cls.pending_root,
            bse_current_delisting_cas_root=cls.bse_root,
            minimum_tdx_code_count=6,
        )
        cls.pending_reference = observation.UnderlyingManifestReference(
            cas_root=cls.pending_root,
            manifest_sha256=pending_reference.manifest_sha256,
            logical_content_sha256=PENDING_LOGICAL_SHA256,
        )
        cls.bse_reference = observation.UnderlyingManifestReference(
            cas_root=cls.bse_root,
            manifest_sha256=bse_reference.manifest_sha256,
            logical_content_sha256=bse_artifact.logical_content_sha256,
        )
        cls.tdx_codes = tuple(
            sorted(
                {
                    *observation.PENDING_REQUIRED_CODES,
                    "000001.SZ",
                    "600000.SH",
                    "920001.BJ",
                }
            )
        )
        cls.tdx_names = {
            code: f"Fixture {code}"
            for code in cls.tdx_codes
        }
        cls.tdx = observation.TDXAShareObservation.capture(
            cls.tdx_names,
            observed_at=FIXED_NOW - timedelta(seconds=5),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.shared_temporary.cleanup()

    def setUp(self) -> None:
        self.runtime_temporary = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.runtime_temporary.name) / "observation"

    def tearDown(self) -> None:
        self.runtime_temporary.cleanup()

    @contextmanager
    def _replayed_artifacts(
        self,
        *,
        pending_artifact: pending_listing.PendingListingArtifact | None = None,
        bse_artifact: bse_current.BSECurrentDelistingArtifact | None = None,
    ):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    pending_listing.PendingListingManifestStore,
                    "replay",
                    return_value=pending_artifact or self.pending_artifact,
                )
            )
            stack.enter_context(
                patch.object(
                    bse_current.BSECurrentDelistingManifestStore,
                    "replay",
                    return_value=bse_artifact or self.bse_artifact,
                )
            )
            yield

    def _assemble(
        self,
        *,
        pending_reference: observation.UnderlyingManifestReference | None = None,
        bse_reference: observation.UnderlyingManifestReference | None = None,
        tdx: observation.TDXAShareObservation | None = None,
        as_of: datetime = FIXED_NOW,
        now: datetime = FIXED_NOW,
    ) -> observation.SecurityMasterObservationBatch:
        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=now
        ):
            return observation.assemble_security_master_observation(
                policy=self.policy,
                pending_manifest=pending_reference or self.pending_reference,
                bse_current_delisting_manifest=bse_reference or self.bse_reference,
                tdx_observation=tdx or self.tdx,
                as_of=as_of,
            )

    def test_happy_path_seals_and_cold_replays_all_three_inputs(self) -> None:
        batch = self._assemble()
        self.assertEqual(batch.status, observation.OBSERVATION_READY)
        self.assertLessEqual(
            batch.observation_span_seconds,
            int(observation.MAX_OBSERVATION_WINDOW.total_seconds()),
        )
        self.assertEqual(batch.tdx_a_share.codes, self.tdx_codes)
        self.assertEqual(dict(batch.tdx_a_share.names), self.tdx_names)
        self.assertEqual(
            batch.tdx_a_share.identity_sha256,
            observation._sha256(
                observation._canonical_json_bytes(self.tdx_names)
            ),
        )
        self.assertEqual(
            batch.to_manifest_dict()["source_contract"][
                "tdx_identity_from_one_stock_list_response"
            ],
            True,
        )
        self.assertEqual(batch.pending_listing.raw_capture_count, 12)
        self.assertGreater(batch.bse_current_delisting.raw_capture_count, 2)

        store = observation.SecurityMasterObservationStore(
            self.runtime_root,
            policy=self.policy,
        )
        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW
        ):
            reference = store.seal(batch)
            replayed = store.replay_current(reference.manifest_sha256)
        self.assertEqual(replayed.to_manifest_dict(), batch.to_manifest_dict())
        self.assertEqual(
            hashlib.sha256(
                Path(reference.object_path).read_bytes()
            ).hexdigest(),
            reference.manifest_sha256,
        )

    def test_read_only_capture_runner_is_injected_and_audit_only(self) -> None:
        calls: list[str] = []

        def capture_pending() -> observation.UnderlyingManifestReference:
            calls.append("pending")
            return self.pending_reference

        def capture_bse() -> observation.UnderlyingManifestReference:
            calls.append("bse")
            return self.bse_reference

        def load_tdx_stock_list() -> list[dict[str, str]]:
            calls.append("tdx")
            return [
                {"Code": code, "Name": name}
                for code, name in reversed(tuple(self.tdx_names.items()))
            ]

        with self._replayed_artifacts(), patch.object(
            observation.SecurityMasterObservationStore,
            "replay_current",
            side_effect=AssertionError("capture runner must use audit replay"),
        ):
            result = observation.capture_current_security_master_observation(
                self.runtime_root,
                policy=self.policy,
                pending_manifest_capture=capture_pending,
                bse_manifest_capture=capture_bse,
                tdx_stock_list_loader=load_tdx_stock_list,
                clock=lambda: FIXED_NOW,
            )

        self.assertEqual(calls, ["pending", "bse", "tdx"])
        self.assertEqual(result["status"], observation.OBSERVATION_READY)
        self.assertEqual(
            result["protocol_version"],
            "cn-security-master-current-observation-v3",
        )
        self.assertEqual(result["replay_mode"], "IMMUTABLE_AUDIT")
        self.assertTrue(result["audit_only"])
        self.assertTrue(result["no_master_publish"])
        self.assertTrue(result["no_training"])
        self.assertTrue(result["no_trading"])
        self.assertEqual(
            result["tdx"],
            {
                "endpoint": "http://127.0.0.1:17709/",
                "method": "get_stock_list",
                "params": {"market": "5", "list_type": 1},
                "observed_at": FIXED_NOW.isoformat(),
                "code_count": len(self.tdx_codes),
                "code_set_sha256": self.tdx.code_set_sha256,
                "identity_sha256": self.tdx.identity_sha256,
            },
        )

    def test_tdx_loader_calls_only_the_read_only_stock_list_method(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def call(self, method: str, params: dict[str, object]) -> object:
                self.calls.append((method, params))
                return [
                    {"Code": "600000.SH", "Name": "Pudong"},
                    {"Code": "000001.SZ", "Name": "Ping An"},
                ]

        client = FakeClient()
        rows = observation._load_current_tdx_stock_list(
            timeout_seconds=7.5,
            client_factory=lambda timeout: (
                client
                if timeout == 7.5
                else self.fail("unexpected TDX timeout")
            ),
        )
        self.assertEqual(
            rows,
            [
                {"Code": "600000.SH", "Name": "Pudong"},
                {"Code": "000001.SZ", "Name": "Ping An"},
            ],
        )
        captured = observation.TDXAShareObservation.capture(
            rows,
            observed_at=FIXED_NOW,
        )
        self.assertEqual(captured.codes, ("000001.SZ", "600000.SH"))
        self.assertEqual(
            dict(captured.names),
            {"000001.SZ": "Ping An", "600000.SH": "Pudong"},
        )
        self.assertEqual(
            client.calls,
            [("get_stock_list", {"market": "5", "list_type": 1})],
        )

    def test_tdx_identity_capture_accepts_real_rows_or_mapping(self) -> None:
        rows = [
            {"Code": "600000.SH", "Name": "浦发银行"},
            {"Code": "000001.SZ", "Name": "平安银行"},
        ]
        from_rows = observation.TDXAShareObservation.capture(
            rows,
            observed_at=FIXED_NOW,
        )
        from_mapping = observation.TDXAShareObservation.capture(
            {"600000.SH": "浦发银行", "000001.SZ": "平安银行"},
            observed_at=FIXED_NOW,
        )
        self.assertEqual(from_rows, from_mapping)
        self.assertEqual(from_rows.codes, ("000001.SZ", "600000.SH"))
        self.assertEqual(
            from_rows.to_dict()["names"],
            {"000001.SZ": "平安银行", "600000.SH": "浦发银行"},
        )
        self.assertEqual(
            from_rows.identity_sha256,
            observation._sha256(
                observation._canonical_json_bytes(from_rows.to_dict()["names"])
            ),
        )

    def test_tdx_identity_missing_duplicate_code_only_and_order_fail_closed(
        self,
    ) -> None:
        bad_captures = (
            [{"Code": "600000.SH"}],
            [{"Code": "600000.SH", "Name": ""}],
            [
                {"Code": "600000.SH", "Name": "浦发银行"},
                {"Code": "600000.SH", "Name": "浦发银行"},
            ],
            [
                {"Code": "600000.SH", "Name": "浦发银行"},
                {"Code": "600000.SH", "Name": "冲突名称"},
            ],
            self.tdx_codes,
        )
        for value in bad_captures:
            with self.subTest(value=value), self.assertRaises(
                observation.SecurityMasterObservationBlockedError
            ):
                observation.TDXAShareObservation.capture(
                    value,
                    observed_at=FIXED_NOW,
                )

        for forged in (
            replace(self.tdx, codes=tuple(reversed(self.tdx.codes))),
            replace(
                self.tdx,
                codes=(*self.tdx.codes, self.tdx.codes[-1]),
            ),
            replace(
                self.tdx,
                names={
                    code: name
                    for code, name in self.tdx.names.items()
                    if code != self.tdx.codes[0]
                },
            ),
            replace(
                self.tdx,
                names={**self.tdx.names, "600001.SH": "额外身份"},
            ),
        ):
            with self.subTest(forged=forged), self.assertRaises(
                observation.SecurityMasterObservationBlockedError
            ):
                observation._validate_tdx_observation(
                    forged,
                    current=FIXED_NOW,
                    minimum_count=6,
                )

    def test_capture_runner_rejects_caller_code_only_input(self) -> None:
        for keyword in ("tdx_stock_list_loader", "tdx_code_loader"):
            with self.subTest(keyword=keyword), self._replayed_artifacts(), (
                self.assertRaisesRegex(
                    observation.SecurityMasterObservationBlockedError,
                    "code-only input is rejected",
                )
            ):
                observation.capture_current_security_master_observation(
                    self.runtime_root,
                    policy=self.policy,
                    pending_manifest_capture=lambda: self.pending_reference,
                    bse_manifest_capture=lambda: self.bse_reference,
                    clock=lambda: FIXED_NOW,
                    **{keyword: lambda: self.tdx_codes},
                )

        with self.assertRaisesRegex(ValueError, "only one TDX"):
            observation.capture_current_security_master_observation(
                self.runtime_root,
                policy=self.policy,
                tdx_stock_list_loader=lambda: self.tdx_names,
                tdx_code_loader=lambda: self.tdx_names,
                clock=lambda: FIXED_NOW,
            )

    def test_manifest_identity_tamper_and_v2_payload_fail_closed(self) -> None:
        batch = self._assemble()
        store = observation.SecurityMasterObservationStore(
            self.runtime_root,
            policy=self.policy,
        )
        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW
        ):
            reference = store.seal(batch)
        original = json.loads(Path(reference.object_path).read_text(encoding="utf-8"))
        attacks = []

        changed_name = json.loads(json.dumps(original))
        first_code = changed_name["tdx_a_share"]["codes"][0]
        changed_name["tdx_a_share"]["names"][first_code] = "伪造名称"
        attacks.append(changed_name)

        changed_hash = json.loads(json.dumps(original))
        changed_hash["tdx_a_share"]["identity_sha256"] = "0" * 64
        attacks.append(changed_hash)

        code_name_mismatch = json.loads(json.dumps(original))
        code_name_mismatch["tdx_a_share"]["names"].pop(first_code)
        attacks.append(code_name_mismatch)

        v2_payload = json.loads(json.dumps(original))
        v2_payload["protocol_version"] = "cn-security-master-current-observation-v2"
        v2_payload["tdx_a_share"].pop("names")
        v2_payload["tdx_a_share"].pop("identity_sha256")
        attacks.append(v2_payload)

        for payload in attacks:
            raw = observation._canonical_json_bytes(payload)
            with self.subTest(payload=payload["protocol_version"]), patch.object(
                observation, "_wall_clock", return_value=FIXED_NOW
            ), self._replayed_artifacts(), self.assertRaises(
                observation.SecurityMasterObservationBlockedError
            ):
                observation._rebuild_observation_manifest(
                    raw,
                    policy=self.policy,
                    require_current=False,
                )

    def test_expired_batch_remains_auditable_but_cannot_pass_current_gate(self) -> None:
        batch = self._assemble()
        store = observation.SecurityMasterObservationStore(
            self.runtime_root,
            policy=self.policy,
        )
        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW + timedelta(seconds=10)
        ):
            reference = store.seal(batch)
            replayed = store.replay(reference.manifest_sha256)
        self.assertEqual(replayed.to_manifest_dict(), batch.to_manifest_dict())

        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW + timedelta(seconds=25)
        ):
            current = store.replay_current(reference.manifest_sha256)
        self.assertEqual(current.to_manifest_dict(), batch.to_manifest_dict())

        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW + timedelta(days=1)
        ):
            historical = store.replay(reference.manifest_sha256)
        self.assertEqual(historical.to_manifest_dict(), batch.to_manifest_dict())

        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW + timedelta(seconds=26)
        ), self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "TDX observation is stale",
        ):
            store.replay_current(reference.manifest_sha256)

    def test_stale_source_and_cross_source_span_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "pending-listing manifest failed cold replay.*stale",
        ):
            self._assemble(
                now=FIXED_NOW + timedelta(minutes=20),
                as_of=FIXED_NOW + timedelta(minutes=20),
                tdx=observation.TDXAShareObservation.capture(
                    self.tdx_names,
                    observed_at=FIXED_NOW + timedelta(minutes=20),
                ),
            )

        with patch.object(
            observation.pending_listing,
            "validate_pending_listing_freshness",
            return_value=None,
        ), patch.object(
            observation.bse_current,
            "validate_current_delisting_freshness",
            return_value=None,
        ):
            old_sources = list(self.pending_artifact.raw_sources)
            old_sources[0] = replace(
                old_sources[0],
                retrieved_at=(FIXED_NOW - timedelta(minutes=6)).isoformat(),
            )
            too_wide = replace(
                self.pending_artifact,
                raw_sources=tuple(old_sources),
            )
            with self.assertRaisesRegex(
                observation.SecurityMasterObservationBlockedError,
                "five-minute observation window",
            ):
                with self._replayed_artifacts(pending_artifact=too_wide), patch.object(
                    observation, "_wall_clock", return_value=FIXED_NOW
                ):
                    observation.assemble_security_master_observation(
                        policy=self.policy,
                        pending_manifest=self.pending_reference,
                        bse_current_delisting_manifest=self.bse_reference,
                        tdx_observation=self.tdx,
                        as_of=FIXED_NOW,
                    )

    def test_re_dated_tdx_and_caller_summary_tamper_are_rejected(self) -> None:
        redated = replace(
            self.tdx,
            observed_at=(FIXED_NOW - timedelta(minutes=1)).isoformat(),
        )
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "TDX observed_at",
        ):
            self._assemble(tdx=redated)

        forged = replace(
            self.pending_reference,
            claimed_earliest_retrieved_at=(FIXED_NOW - timedelta(seconds=1)).isoformat(),
            claimed_latest_retrieved_at=FIXED_NOW.isoformat(),
        )
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "caller earliest-time summary is forged",
        ):
            self._assemble(pending_reference=forged)

    def test_tdx_summary_code_membership_and_terminated_presence_are_rejected(self) -> None:
        wrong_hash = replace(self.tdx, code_set_sha256="0" * 64)
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "count/hash summar",
        ):
            self._assemble(tdx=wrong_hash)

        wrong_identity_hash = replace(self.tdx, identity_sha256="0" * 64)
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "count/hash summar",
        ):
            self._assemble(tdx=wrong_identity_hash)

        changed_name = replace(
            self.tdx,
            names={
                **self.tdx.names,
                self.tdx.codes[0]: "伪造名称",
            },
        )
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "count/hash summar",
        ):
            self._assemble(tdx=changed_name)

        missing_pending_codes = tuple(
            item for item in self.tdx_codes if item != "301655.SZ"
        )
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "missing pending assigned codes",
        ):
            self._assemble(
                tdx=observation.TDXAShareObservation.capture(
                    {
                        code: self.tdx_names[code]
                        for code in missing_pending_codes
                    },
                    observed_at=FIXED_NOW - timedelta(seconds=5),
                )
            )

        includes_terminated = tuple(sorted((*self.tdx_codes, "920305.BJ")))
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "terminated BSE codes",
        ):
            self._assemble(
                tdx=observation.TDXAShareObservation.capture(
                    {
                        **self.tdx_names,
                        "920305.BJ": "Terminated Fixture",
                    },
                    observed_at=FIXED_NOW - timedelta(seconds=5),
                )
            )

    def test_underlying_cas_tamper_is_detected_during_replay(self) -> None:
        tamper_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(tamper_temporary.cleanup)
        tamper_root = Path(tamper_temporary.name) / "pending"
        shutil.copytree(self.pending_root, tamper_root)
        tamper_policy = observation.SecurityMasterObservationPolicy(
            pending_cas_root=tamper_root,
            bse_current_delisting_cas_root=self.bse_root,
            minimum_tdx_code_count=6,
        )
        tamper_reference = replace(self.pending_reference, cas_root=tamper_root)
        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW
        ):
            batch = observation.assemble_security_master_observation(
                policy=tamper_policy,
                pending_manifest=tamper_reference,
                bse_current_delisting_manifest=self.bse_reference,
                tdx_observation=self.tdx,
                as_of=FIXED_NOW,
            )
        store = observation.SecurityMasterObservationStore(
            self.runtime_root,
            policy=tamper_policy,
        )
        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW
        ):
            reference = store.seal(batch)

        manifest_path = (
            tamper_root
            / "sha256"
            / tamper_reference.manifest_sha256[:2]
            / tamper_reference.manifest_sha256
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_digest = payload["sources"][0]["content_sha256"]
        source_path = (
            tamper_root / "sha256" / source_digest[:2] / source_digest
        )
        source_path.write_bytes(b"tampered")

        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "failed cold replay",
        ):
            with patch.object(observation, "_wall_clock", return_value=FIXED_NOW):
                store.replay(reference.manifest_sha256)

    def test_sealed_payload_tamper_and_reparse_path_are_rejected(self) -> None:
        batch = self._assemble()
        store = observation.SecurityMasterObservationStore(
            self.runtime_root,
            policy=self.policy,
        )
        with self._replayed_artifacts(), patch.object(
            observation, "_wall_clock", return_value=FIXED_NOW
        ):
            reference = store.seal(batch)
        path = Path(reference.object_path)
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            observation.SecurityMasterObservationBlockedError,
            "hash mismatch",
        ):
            with self._replayed_artifacts(), patch.object(
                observation, "_wall_clock", return_value=FIXED_NOW
            ):
                store.replay(reference.manifest_sha256)

        class _ReparseStat:
            def __init__(self, original: object) -> None:
                self._original = original
                self.st_file_attributes = int(
                    getattr(original, "st_file_attributes", 0)
                ) | 0x00000400

            def __getattr__(self, name: str) -> object:
                return getattr(self._original, name)

        real_lstat = observation.os.lstat
        unsafe = self.pending_root

        def fake_lstat(value: object) -> object:
            result = real_lstat(value)
            if Path(value).absolute() == unsafe.absolute():
                return _ReparseStat(result)
            return result

        with patch.object(observation.os, "lstat", side_effect=fake_lstat):
            with self.assertRaisesRegex(
                observation.SecurityMasterObservationBlockedError,
                "reparse",
            ):
                self._assemble()


if __name__ == "__main__":
    unittest.main()
