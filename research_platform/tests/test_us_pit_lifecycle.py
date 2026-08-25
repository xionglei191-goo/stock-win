from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research_platform.us_pit.hashing import sha256_bytes, sha256_json
from research_platform.us_pit.lifecycle import (
    LIFECYCLE_FORMAT_VERSION,
    lifecycle_evidence_adapter,
    load_lifecycle_surveillance,
)
from research_platform.us_pit.models import (
    LicenseClass,
    SourceDependency,
    SourceRole,
)
from research_platform.us_pit.sources import SyncRequest
from research_platform.us_pit.store import USPITStore


class USPITLifecycleTests(unittest.TestCase):
    def _write(
        self,
        root: Path,
        ids: list[str],
        *,
        evidence_sha256: str,
        asserted: str | None = None,
        record_ids: list[str] | None = None,
    ) -> Path:
        normalized_record_ids = sorted(record_ids or ids)
        observations = [
            {
                "security_id": security_id,
                "identifier_type": "CUSIP",
                "identifier_value": f"00000000{security_id[-1].upper()}",
                "observed_status": "LISTED",
                "evidence_locator": f"fixture:{security_id}",
                "observed_through": "2026-07-31",
                "status_effective_at": "",
                "evidence_excerpt": "official lifecycle fixture",
            }
            for security_id in normalized_record_ids
        ]
        source_records = [
            {
                "source_id": "sec-lifecycle-source",
                "dataset": "lifecycle_observation",
                "url": "https://www.sec.gov/Archives/example.txt",
                "published_at": "2026-08-01T00:00:00+00:00",
                "evidence_sha256": evidence_sha256,
                "observations": observations,
            }
        ]
        path = root / "lifecycle.json"
        path.write_text(
            json.dumps(
                {
                    "format_version": LIFECYCLE_FORMAT_VERSION,
                    "current_through": "2026-07-31",
                    "covered_security_ids": ids,
                    "covered_security_ids_sha256": asserted or sha256_json(sorted(ids)),
                    "source_records": source_records,
                    "source_records_sha256": sha256_json(source_records),
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _source_batch(store: USPITStore, payload: bytes):
        reference = store.put_bytes(payload)
        return store.write_source_batch(
            [
                SourceDependency(
                    source_id="sec-lifecycle-source",
                    source_version="2026-08-01",
                    role=SourceRole.SIGNAL_INPUT,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    object_sha256=reference.sha256,
                    observed_at="2026-08-01T01:00:00+00:00",
                    published_at="2026-08-01T00:00:00+00:00",
                    url="https://www.sec.gov/Archives/example.txt",
                    dataset="lifecycle_observation",
                )
            ]
        )

    def test_coverage_hash_is_derived_from_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = USPITStore(root / "store")
            payload = b"official lifecycle fixture 00000000A 00000000B"
            batch = self._source_batch(store, payload)
            digest = sha256_bytes(payload)
            path = self._write(
                root,
                ["us_cusip_b", "us_cusip_a"],
                evidence_sha256=digest,
            )
            document = load_lifecycle_surveillance(path)
            self.assertEqual(("us_cusip_a", "us_cusip_b"), document.covered_security_ids)
            adapter = lifecycle_evidence_adapter(
                path=path,
                source_id="official_lifecycle",
                source_version="2026-07-31",
                public_url="https://www.sec.gov/Archives/example.txt",
                published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                store=store,
                source_batch_ids=[batch.batch_id],
            )
            artifact = tuple(
                adapter.fetch(
                    SyncRequest(
                        start_date=document.current_through,
                        end_date=datetime.now(timezone.utc).date(),
                        observed_at=datetime.now(timezone.utc),
                    )
                )
            )[0]
            self.assertEqual(
                document.covered_security_ids_sha256,
                artifact.metadata["covered_security_ids_sha256"],
            )
            self.assertEqual(3, artifact.metadata["coverage_contract_version"])
            self.assertTrue(artifact.metadata["source_records_bound_to_cas"])
            self.assertTrue(
                artifact.metadata["observation_identifiers_verified_in_payload"]
            )

    def test_caller_cannot_self_attest_a_different_coverage_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                Path(directory),
                ["us_cusip_a"],
                evidence_sha256="a" * 64,
                asserted="b" * 64,
            )
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                load_lifecycle_surveillance(path)

    def test_coverage_cannot_exceed_record_union_or_reference_uncaptured_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = USPITStore(root / "store")
            payload = b"official lifecycle fixture 00000000A 00000000B"
            batch = self._source_batch(store, payload)
            path = self._write(
                root,
                ["us_cusip_a", "us_cusip_b"],
                record_ids=["us_cusip_a"],
                evidence_sha256=sha256_bytes(payload),
            )
            with self.assertRaisesRegex(ValueError, "must equal the union"):
                load_lifecycle_surveillance(path)

            path = self._write(
                root,
                ["us_cusip_a"],
                evidence_sha256="f" * 64,
            )
            with self.assertRaisesRegex(ValueError, "exactly one captured"):
                lifecycle_evidence_adapter(
                    path=path,
                    source_id="official_lifecycle",
                    source_version="2026-07-31",
                    public_url="https://www.sec.gov/Archives/example.txt",
                    published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                    store=store,
                    source_batch_ids=[batch.batch_id],
                )

    def test_identifier_must_exist_in_the_frozen_source_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = USPITStore(root / "store")
            payload = b"official lifecycle fixture without the identifier"
            batch = self._source_batch(store, payload)
            path = self._write(
                root,
                ["us_cusip_a"],
                evidence_sha256=sha256_bytes(payload),
            )
            with self.assertRaisesRegex(ValueError, "identifier is absent"):
                lifecycle_evidence_adapter(
                    path=path,
                    source_id="official_lifecycle",
                    source_version="2026-07-31",
                    public_url="https://www.sec.gov/Archives/example.txt",
                    published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                    store=store,
                    source_batch_ids=[batch.batch_id],
                )


if __name__ == "__main__":
    unittest.main()
