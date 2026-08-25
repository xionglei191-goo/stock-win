from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests

from research_platform import cninfo_delisted_disclosures as cninfo
from research_platform.cninfo_announcement_quality_adapter import (
    AnnouncementDocumentsQualityIndexReference,
    build_cninfo_announcement_documents_quality_index,
    replay_cninfo_announcement_documents_quality_index,
)
from research_platform.historical_security_master import (
    HISTORICAL_SECURITY_MASTER_STORE_ROOT,
    HistoricalSecurityMasterBlockedError,
    HistoricalSecurityMasterStore,
    SecurityMasterRecord,
    validate_security_master_records,
)


PROTOCOL_VERSION = "cninfo-announcement-capture-coordinator-v1"
CHECKPOINT_PROTOCOL_VERSION = "cninfo-announcement-target-checkpoint-v1"
WORK_PLAN_PROTOCOL_VERSION = "cninfo-announcement-work-plan-v1"
PAGE_CHECKPOINT_PROTOCOL_VERSION = "cninfo-announcement-page-checkpoint-v1"
DOCUMENT_CHECKPOINT_PROTOCOL_VERSION = "cninfo-announcement-document-checkpoint-v1"
PARSE_CHECKPOINT_PROTOCOL_VERSION = "cninfo-announcement-parse-checkpoint-v1"
AUDIT_START = date(2018, 1, 1)
AUDIT_END_EXCLUSIVE = date(2024, 1, 1)
EXPECTED_SZSE_TARGET_COUNT = 140


class CninfoAnnouncementCaptureBlockedError(RuntimeError):
    """The resumable CNINFO capture cannot preserve its frozen scope."""


@dataclass(frozen=True)
class AuthoritativeAnnouncementScope:
    master_snapshot_id: str
    master_content_sha256: str
    master_row_count: int
    targets: tuple[cninfo.FrozenDisclosureTarget, ...]
    scope_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "targets": [item.to_dict() for item in self.targets],
        }


@dataclass(frozen=True)
class AnnouncementCaptureProgress:
    protocol_version: str
    master_snapshot_id: str
    scope_sha256: str
    target_count: int
    authoritative_target_count: int
    captured_count: int
    missing_count: int
    captured_codes: tuple[str, ...]
    missing_codes: tuple[str, ...]
    manifest_sha256_by_code: Mapping[str, str]
    selected_complete: bool
    full_authoritative_scope: bool
    complete: bool
    in_progress_codes: tuple[str, ...] = ()
    planned_page_count: int = 0
    checkpointed_page_count: int = 0
    planned_document_count: int = 0
    checkpointed_document_count: int = 0
    checkpointed_parse_count: int = 0
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["manifest_sha256_by_code"] = dict(
            sorted(self.manifest_sha256_by_code.items())
        )
        value["in_progress_codes"] = list(self.in_progress_codes)
        return value


@dataclass(frozen=True)
class AnnouncementIndexMaterialization:
    master_snapshot_id: str
    scope_sha256: str
    selected_target_count: int
    authoritative_target_count: int
    selected_codes: tuple[str, ...]
    full_authoritative_scope: bool
    disclosure_manifest_sha256: str
    disclosure_logical_content_sha256: str
    quality_index: AnnouncementDocumentsQualityIndexReference
    copied_raw_object_count: int = 0
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "quality_index": self.quality_index.to_dict(),
        }


def load_authoritative_szse_announcement_scope(
    *,
    master_store_root: Path = HISTORICAL_SECURITY_MASTER_STORE_ROOT,
    expected_snapshot_id: str | None = None,
) -> AuthoritativeAnnouncementScope:
    """Cold-load the promoted master and derive the exact SZSE audit scope.

    The target count is a frozen policy assertion for this research release,
    not a caller option.  A changed master must be reviewed before capture can
    continue under a new coordinator protocol.
    """

    root = Path(master_store_root).absolute()
    try:
        release = HistoricalSecurityMasterStore(root).load_current_release()
    except (HistoricalSecurityMasterBlockedError, OSError, ValueError) as exc:
        raise CninfoAnnouncementCaptureBlockedError(
            f"authoritative security master failed cold replay: {exc}"
        ) from exc
    snapshot_id = _sha256_identity(
        release.get("snapshot_id"), "security-master snapshot"
    )
    if expected_snapshot_id is not None and snapshot_id != _sha256_identity(
        expected_snapshot_id, "expected security-master snapshot"
    ):
        raise CninfoAnnouncementCaptureBlockedError(
            "promoted security-master snapshot changed during announcement capture"
        )
    try:
        metadata = dict(
            release["manifest"]["artifacts"]["security_master_jsonl"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CninfoAnnouncementCaptureBlockedError(
            "security-master release has no JSONL artifact"
        ) from exc
    master_hash = _sha256_identity(
        metadata.get("content_hash"), "security-master content"
    )
    expected_path = root / "objects" / master_hash[:2] / master_hash
    object_path = Path(str(metadata.get("object_path") or ""))
    if object_path.resolve() != expected_path.resolve():
        raise CninfoAnnouncementCaptureBlockedError(
            "security-master object path does not match its content hash"
        )
    try:
        raw = cninfo._stable_read(root, object_path)
    except (cninfo.CninfoDelistedDisclosureBlockedError, OSError) as exc:
        raise CninfoAnnouncementCaptureBlockedError(
            f"security-master JSONL cannot be read stably: {exc}"
        ) from exc
    if _sha256(raw) != master_hash:
        raise CninfoAnnouncementCaptureBlockedError(
            "security-master JSONL content hash mismatch"
        )
    records = _parse_master_jsonl(raw)
    declared_count = metadata.get("row_count")
    if type(declared_count) is not int or declared_count != len(records):
        raise CninfoAnnouncementCaptureBlockedError(
            "security-master JSONL row count mismatch"
        )
    try:
        validate_security_master_records(records)
    except HistoricalSecurityMasterBlockedError as exc:
        raise CninfoAnnouncementCaptureBlockedError(
            f"security-master records failed validation: {exc}"
        ) from exc
    targets = _derive_szse_targets(records)
    if len(targets) != EXPECTED_SZSE_TARGET_COUNT:
        raise CninfoAnnouncementCaptureBlockedError(
            "frozen SZSE announcement target count changed: "
            f"expected {EXPECTED_SZSE_TARGET_COUNT}, got {len(targets)}"
        )
    scope_payload = {
        "master_snapshot_id": snapshot_id,
        "master_content_sha256": master_hash,
        "targets": [item.to_dict() for item in targets],
    }
    return AuthoritativeAnnouncementScope(
        master_snapshot_id=snapshot_id,
        master_content_sha256=master_hash,
        master_row_count=len(records),
        targets=targets,
        scope_sha256=_sha256(_canonical_json_bytes(scope_payload)),
    )


class CninfoAnnouncementCaptureCoordinator:
    """Capture one target at a time and assemble only cold-replayed evidence."""

    def __init__(
        self,
        *,
        cas_root: Path,
        checkpoint_root: Path,
        master_store_root: Path = HISTORICAL_SECURITY_MASTER_STORE_ROOT,
        expected_master_snapshot_id: str | None = None,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.cas = cninfo.CninfoDisclosureCAS(Path(cas_root))
        self.checkpoint_root = Path(checkpoint_root).absolute()
        cninfo._prepare_root(self.checkpoint_root)
        self.scope = load_authoritative_szse_announcement_scope(
            master_store_root=master_store_root,
            expected_snapshot_id=expected_master_snapshot_id,
        )
        self.client = cninfo.CninfoDelistedDisclosureClient(
            cas=self.cas,
            session=session,
            timeout_seconds=timeout_seconds,
        )
        self.manifest_store = cninfo.CninfoDelistedDisclosureManifestStore(
            self.cas
        )

    def capture(
        self,
        *,
        codes: Sequence[str] | None = None,
        max_new_targets: int | None = None,
    ) -> AnnouncementCaptureProgress:
        selected = self._select_targets(codes)
        if max_new_targets is not None and (
            type(max_new_targets) is not int or max_new_targets < 0
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                "max_new_targets must be a non-negative integer"
            )
        new_count = 0
        verified_manifests: dict[str, str] = {}
        for target in selected:
            existing = self._load_target_checkpoint(target, required=False)
            if existing is not None:
                verified_manifests[target.code] = existing[0]
                continue
            if max_new_targets is not None and new_count >= max_new_targets:
                continue
            try:
                reference = self._capture_target_resumable(target)
            except cninfo.CninfoDelistedDisclosureBlockedError as exc:
                raise CninfoAnnouncementCaptureBlockedError(
                    f"CNINFO capture failed for {target.code}: {exc}"
                ) from exc
            self._write_target_checkpoint(target, reference)
            verified_manifests[target.code] = reference.manifest_sha256
            new_count += 1
        return self._progress_from_verified(selected, verified_manifests)

    def progress(
        self, *, codes: Sequence[str] | None = None
    ) -> AnnouncementCaptureProgress:
        selected = self._select_targets(codes)
        manifests: dict[str, str] = {}
        for target in selected:
            checkpoint = self._load_target_checkpoint(target, required=False)
            if checkpoint is not None:
                manifests[target.code] = checkpoint[0]
        return self._progress_from_verified(selected, manifests)

    def _progress_from_verified(
        self,
        selected: tuple[cninfo.FrozenDisclosureTarget, ...],
        manifests: Mapping[str, str],
    ) -> AnnouncementCaptureProgress:
        missing = [
            target.code for target in selected if target.code not in manifests
        ]
        captured = tuple(sorted(manifests))
        work = [
            self._work_progress(target)
            for target in selected
            if target.code not in manifests
        ]
        selected_complete = not missing
        full_scope = selected == self.scope.targets
        return AnnouncementCaptureProgress(
            protocol_version=PROTOCOL_VERSION,
            master_snapshot_id=self.scope.master_snapshot_id,
            scope_sha256=self.scope.scope_sha256,
            target_count=len(selected),
            authoritative_target_count=len(self.scope.targets),
            captured_count=len(captured),
            missing_count=len(missing),
            captured_codes=captured,
            missing_codes=tuple(sorted(missing)),
            manifest_sha256_by_code=manifests,
            selected_complete=selected_complete,
            full_authoritative_scope=full_scope,
            complete=selected_complete and full_scope,
            in_progress_codes=tuple(
                item["code"] for item in work if item["in_progress"]
            ),
            planned_page_count=sum(item["planned_pages"] for item in work),
            checkpointed_page_count=sum(
                item["checkpointed_pages"] for item in work
            ),
            planned_document_count=sum(
                item["planned_documents"] for item in work
            ),
            checkpointed_document_count=sum(
                item["checkpointed_documents"] for item in work
            ),
            checkpointed_parse_count=sum(
                item["checkpointed_parses"] for item in work
            ),
        )

    def _work_progress(
        self, target: cninfo.FrozenDisclosureTarget
    ) -> dict[str, Any]:
        plan = self._load_work_plan(target, required=False)
        if plan is None:
            return {
                "code": target.code,
                "in_progress": False,
                "planned_pages": 0,
                "checkpointed_pages": 0,
                "planned_documents": 0,
                "checkpointed_documents": 0,
                "checkpointed_parses": 0,
            }
        _stock_master, org_id, page_count, total = self._replay_work_plan(
            target, plan
        )
        plan_hash = _sha256(_canonical_json_bytes(plan))
        rows: list[dict[str, Any]] = []
        checkpointed_pages = 0
        for page_num in range(1, page_count + 1):
            page = self._load_page_checkpoint(
                target,
                org_id=org_id,
                page_num=page_num,
                page_count=page_count,
                total=total,
                work_plan_sha256=plan_hash,
                required=False,
            )
            if page is None:
                continue
            checkpointed_pages += 1
            rows.extend(
                self._replay_page(
                    target,
                    page_value=page,
                    org_id=org_id,
                    page_count=page_count,
                    total=total,
                )
            )
        checkpointed_documents = 0
        checkpointed_parses = 0
        for row in rows:
            document = self._load_document_checkpoint(
                target,
                row=row,
                work_plan_sha256=plan_hash,
                required=False,
            )
            if document is None:
                continue
            checkpointed_documents += 1
            if (
                self._load_parse_checkpoint(
                    target,
                    row=row,
                    document=document,
                    work_plan_sha256=plan_hash,
                    required=False,
                )
                is not None
            ):
                checkpointed_parses += 1
        return {
            "code": target.code,
            "in_progress": True,
            "planned_pages": page_count,
            "checkpointed_pages": checkpointed_pages,
            "planned_documents": total,
            "checkpointed_documents": checkpointed_documents,
            "checkpointed_parses": checkpointed_parses,
        }

    def admit_existing_target_manifest(
        self, *, code: str, manifest_sha256: str
    ) -> AnnouncementCaptureProgress:
        """Recover a sealed target manifest after an interrupted checkpoint.

        The supplied digest is only a lookup key.  The manifest and every raw
        dependency are cold-replayed before a checkpoint can be written.
        """

        target = self._select_targets([code])[0]
        manifest_hash = _sha256_identity(
            manifest_sha256, "CNINFO target manifest"
        )
        try:
            manifest_bytes, manifest_path = self.cas.read_blob(manifest_hash)
            artifact = self.manifest_store.replay(manifest_hash)
        except cninfo.CninfoDelistedDisclosureBlockedError as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO target manifest failed cold admission for {target.code}: {exc}"
            ) from exc
        if (
            artifact.master_snapshot_id != self.scope.master_snapshot_id
            or artifact.targets != (target,)
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO target manifest scope mismatch for {target.code}"
            )
        reference = cninfo.CninfoDelistedDisclosureManifestReference(
            manifest_sha256=manifest_hash,
            byte_count=len(manifest_bytes),
            cas_uri=f"sha256:{manifest_hash}",
            object_path=str(manifest_path),
        )
        self._write_target_checkpoint(target, reference)
        replayed = self._load_target_checkpoint(target, required=True)
        assert replayed is not None
        return self.progress(codes=[target.code])

    def materialize_quality_index(
        self,
        *,
        calendar_manifest_sha256: str,
        codes: Sequence[str] | None = None,
        require_full_authoritative_scope: bool = True,
        target_cas_root: Path | None = None,
    ) -> AnnouncementIndexMaterialization:
        selected = self._select_targets(codes)
        full_scope = selected == self.scope.targets
        if require_full_authoritative_scope and not full_scope:
            raise CninfoAnnouncementCaptureBlockedError(
                "partial target selection cannot satisfy full-scope materialization"
            )
        artifacts: list[cninfo.CninfoDelistedDisclosureArtifact] = []
        for target in selected:
            checkpoint = self._load_target_checkpoint(target, required=True)
            assert checkpoint is not None
            artifacts.append(checkpoint[1])
        target_root = (
            self.cas.root
            if target_cas_root is None
            else Path(target_cas_root).absolute()
        )
        target_cas = cninfo.CninfoDisclosureCAS(target_root)
        aggregate, copied_count = self._rebuild_aggregate(
            selected,
            artifacts,
            target_cas=target_cas,
        )
        target_manifest_store = cninfo.CninfoDelistedDisclosureManifestStore(
            target_cas
        )
        try:
            disclosure_reference = target_manifest_store.seal(aggregate)
            replayed_disclosure = target_manifest_store.replay(
                disclosure_reference.manifest_sha256
            )
            quality_reference = build_cninfo_announcement_documents_quality_index(
                cas_root=target_cas.root,
                cninfo_manifest_sha256=disclosure_reference.manifest_sha256,
                calendar_manifest_sha256=calendar_manifest_sha256,
                authoritative_master_snapshot_id=self.scope.master_snapshot_id,
                authoritative_targets=selected,
            )
            replayed_index = replay_cninfo_announcement_documents_quality_index(
                cas_root=target_cas.root,
                source_index_sha256=quality_reference.content_hash,
                cninfo_manifest_sha256=disclosure_reference.manifest_sha256,
                calendar_manifest_sha256=calendar_manifest_sha256,
                authoritative_master_snapshot_id=self.scope.master_snapshot_id,
                authoritative_targets=selected,
            )
        except (
            cninfo.CninfoDelistedDisclosureBlockedError,
            RuntimeError,
            OSError,
            ValueError,
        ) as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"announcement quality index failed cold materialization: {exc}"
            ) from exc
        if replayed_index != quality_reference:
            raise CninfoAnnouncementCaptureBlockedError(
                "announcement quality index replay identity mismatch"
            )
        return AnnouncementIndexMaterialization(
            master_snapshot_id=self.scope.master_snapshot_id,
            scope_sha256=self.scope.scope_sha256,
            selected_target_count=len(selected),
            authoritative_target_count=len(self.scope.targets),
            selected_codes=tuple(item.code for item in selected),
            full_authoritative_scope=full_scope,
            disclosure_manifest_sha256=disclosure_reference.manifest_sha256,
            disclosure_logical_content_sha256=(
                replayed_disclosure.logical_content_sha256
            ),
            quality_index=quality_reference,
            copied_raw_object_count=copied_count,
        )

    def _select_targets(
        self, codes: Sequence[str] | None
    ) -> tuple[cninfo.FrozenDisclosureTarget, ...]:
        if codes is None:
            return self.scope.targets
        if not isinstance(codes, Sequence) or isinstance(codes, (str, bytes)):
            raise CninfoAnnouncementCaptureBlockedError(
                "capture codes must be a sequence"
            )
        normalized = tuple(str(value).strip().upper() for value in codes)
        if not normalized or len(set(normalized)) != len(normalized):
            raise CninfoAnnouncementCaptureBlockedError(
                "capture codes are empty or duplicated"
            )
        targets_by_code = {item.code: item for item in self.scope.targets}
        unknown = sorted(set(normalized) - set(targets_by_code))
        if unknown:
            raise CninfoAnnouncementCaptureBlockedError(
                f"capture codes are outside the authoritative scope: {unknown}"
            )
        return tuple(targets_by_code[code] for code in sorted(normalized))

    def _checkpoint_directory(
        self, target: cninfo.FrozenDisclosureTarget
    ) -> Path:
        return (
            self.checkpoint_root
            / self.scope.master_snapshot_id[:16]
            / target.code
        )

    def _work_directory(self, target: cninfo.FrozenDisclosureTarget) -> Path:
        return self._checkpoint_directory(target) / "_work"

    def _capture_target_resumable(
        self,
        target: cninfo.FrozenDisclosureTarget,
    ) -> cninfo.CninfoDelistedDisclosureManifestReference:
        plan = self._load_work_plan(target, required=False)
        if plan is None:
            stock_master, master_rows = self.client.capture_stock_master()
            master_row = master_rows.get(target.code[:6])
            if master_row is None:
                raise CninfoAnnouncementCaptureBlockedError(
                    f"CNINFO stock master has no frozen target: {target.code}"
                )
            org_id = str(master_row["orgId"])
            _summary, _rows, first_page = self.client.capture_announcement_page(
                target=target,
                org_id=org_id,
                page=1,
            )
            plan = self._freeze_work_plan(
                target,
                stock_master=stock_master.to_dict(),
                org_id=org_id,
                first_page=first_page,
            )
        stock_master, org_id, page_count, total = self._replay_work_plan(
            target, plan
        )
        work_plan_sha256 = _sha256(_canonical_json_bytes(plan))
        pages: list[dict[str, Any]] = []
        all_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for page_num in range(1, page_count + 1):
            page_value = self._load_page_checkpoint(
                target,
                org_id=org_id,
                page_num=page_num,
                page_count=page_count,
                total=total,
                work_plan_sha256=work_plan_sha256,
                required=False,
            )
            if page_value is None:
                if page_num == 1:
                    page_value = dict(plan["first_page"])
                else:
                    _summary, _rows, page_value = (
                        self.client.capture_announcement_page(
                            target=target,
                            org_id=org_id,
                            page=page_num,
                        )
                    )
                self._write_page_checkpoint(
                    target,
                    page_num=page_num,
                    page_value=page_value,
                    work_plan_sha256=work_plan_sha256,
                )
                page_value = self._load_page_checkpoint(
                    target,
                    org_id=org_id,
                    page_num=page_num,
                    page_count=page_count,
                    total=total,
                    work_plan_sha256=work_plan_sha256,
                    required=True,
                )
                assert page_value is not None
            page_rows = self._replay_page(
                target,
                page_value=page_value,
                org_id=org_id,
                page_count=page_count,
                total=total,
            )
            for row in page_rows:
                announcement_id = str(row["announcementId"])
                if announcement_id in seen_ids:
                    raise CninfoAnnouncementCaptureBlockedError(
                        f"duplicate announcementId: {announcement_id}"
                    )
                seen_ids.add(announcement_id)
                all_rows.append(row)
            pages.append(page_value)
        if len(all_rows) != total:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO pagination did not reproduce totalAnnouncement for {target.code}"
            )
        documents: list[dict[str, Any]] = []
        parse_evidence: dict[str, dict[str, Any]] = {}
        total_pdf_bytes = 0
        for row in all_rows:
            announcement_id = str(row["announcementId"])
            document = self._load_document_checkpoint(
                target,
                row=row,
                work_plan_sha256=work_plan_sha256,
                required=False,
            )
            if document is None:
                document = self.client.capture_document(target=target, row=row)
                self._write_document_checkpoint(
                    target,
                    document=document,
                    work_plan_sha256=work_plan_sha256,
                )
                document = self._load_document_checkpoint(
                    target,
                    row=row,
                    work_plan_sha256=work_plan_sha256,
                    required=True,
                )
                assert document is not None
            evidence = cninfo._raw_from_mapping(document["raw"])
            total_pdf_bytes += evidence.byte_count
            if total_pdf_bytes > cninfo.MAX_TOTAL_PDF_BYTES:
                raise CninfoAnnouncementCaptureBlockedError(
                    f"CNINFO PDF bytes exceed safety limit for {target.code}"
                )
            if document["announcement_id"] != announcement_id:
                raise CninfoAnnouncementCaptureBlockedError(
                    f"CNINFO document checkpoint identity mismatch for {announcement_id}"
                )
            documents.append(document)
            parsed = self._load_parse_checkpoint(
                target,
                row=row,
                document=document,
                work_plan_sha256=work_plan_sha256,
                required=False,
            )
            if parsed is None:
                parsed = self._parse_document(
                    target,
                    row=row,
                    document=document,
                )
                self._write_parse_checkpoint(
                    target,
                    announcement_id=announcement_id,
                    parse_evidence=parsed,
                    work_plan_sha256=work_plan_sha256,
                )
                parsed = self._load_parse_checkpoint(
                    target,
                    row=row,
                    document=document,
                    work_plan_sha256=work_plan_sha256,
                    required=True,
                )
                assert parsed is not None
            parse_evidence[announcement_id] = parsed
        try:
            artifact = cninfo._rebuild_artifact(
                cas=self.cas,
                master_snapshot_id=self.scope.master_snapshot_id,
                targets=[target],
                stock_master=stock_master,
                query_pages=pages,
                documents=documents,
                parse_evidence_by_announcement_id=parse_evidence,
            )
            reference = self.manifest_store.seal_candidate_for_full_replay(artifact)
            replayed = self.manifest_store.replay(reference.manifest_sha256)
        except cninfo.CninfoDelistedDisclosureBlockedError as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO resumable capture failed for {target.code}: {exc}"
            ) from exc
        if replayed.targets != (target,):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO sealed target scope changed for {target.code}"
            )
        return reference

    def _freeze_work_plan(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        stock_master: Mapping[str, Any],
        org_id: str,
        first_page: Mapping[str, Any],
    ) -> dict[str, Any]:
        master_evidence = cninfo._raw_from_mapping(stock_master)
        master_raw, _ = self.cas.read_blob(
            master_evidence.content_hash,
            expected_path=master_evidence.object_path,
        )
        if len(master_raw) != master_evidence.byte_count:
            raise CninfoAnnouncementCaptureBlockedError(
                "CNINFO stock-master byte count changed before plan freeze"
            )
        master_rows = cninfo.parse_cninfo_stock_master(master_raw)
        if str(master_rows.get(target.code[:6], {}).get("orgId") or "") != org_id:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO stock-master binding changed for {target.code}"
            )
        raw = cninfo._raw_from_mapping(first_page["raw"])
        page_bytes, _ = self.cas.read_blob(
            raw.content_hash,
            expected_path=raw.object_path,
        )
        summary, _rows = cninfo._parse_announcement_page(
            page_bytes,
            target=target,
            org_id=org_id,
        )
        total = int(summary["total"])
        page_count = max(1, math.ceil(total / cninfo.PAGE_SIZE))
        self._validate_page_summary(
            summary,
            page_num=1,
            page_count=page_count,
            total=total,
        )
        plan = {
            "protocol_version": WORK_PLAN_PROTOCOL_VERSION,
            "master_snapshot_id": self.scope.master_snapshot_id,
            "master_scope_sha256": self.scope.scope_sha256,
            "target": target.to_dict(),
            "stock_master": dict(stock_master),
            "org_id": org_id,
            "page_size": cninfo.PAGE_SIZE,
            "page_count": page_count,
            "document_count": total,
            "first_page": dict(first_page),
            "caller_ready_accepted": False,
        }
        path = self._work_directory(target) / "plan.json"
        cninfo._atomic_write_exact(
            self.checkpoint_root,
            path,
            _canonical_json_bytes(plan),
        )
        return self._load_work_plan(target, required=True) or {}

    def _load_work_plan(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        required: bool,
    ) -> dict[str, Any] | None:
        path = self._work_directory(target) / "plan.json"
        value = self._read_json_checkpoint(path, required=required)
        if value is None:
            return None
        expected = {
            "protocol_version",
            "master_snapshot_id",
            "master_scope_sha256",
            "target",
            "stock_master",
            "org_id",
            "page_size",
            "page_count",
            "document_count",
            "first_page",
            "caller_ready_accepted",
        }
        if (
            set(value) != expected
            or value.get("protocol_version") != WORK_PLAN_PROTOCOL_VERSION
            or value.get("master_snapshot_id") != self.scope.master_snapshot_id
            or value.get("master_scope_sha256") != self.scope.scope_sha256
            or value.get("target") != target.to_dict()
            or value.get("page_size") != cninfo.PAGE_SIZE
            or type(value.get("page_count")) is not int
            or value["page_count"] <= 0
            or value["page_count"] > cninfo.MAX_ANNOUNCEMENT_PAGES_PER_CODE
            or type(value.get("document_count")) is not int
            or value["document_count"] < 0
            or value["document_count"] > cninfo.MAX_DOCUMENTS
            or not isinstance(value.get("first_page"), dict)
            or value.get("caller_ready_accepted") is not False
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO work-plan identity mismatch for {target.code}"
            )
        return value

    def _replay_work_plan(
        self,
        target: cninfo.FrozenDisclosureTarget,
        plan: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str, int, int]:
        try:
            evidence = cninfo._raw_from_mapping(plan["stock_master"])
            raw, _ = self.cas.read_blob(
                evidence.content_hash,
                expected_path=evidence.object_path,
            )
            master_rows = cninfo.parse_cninfo_stock_master(raw)
        except (KeyError, cninfo.CninfoDelistedDisclosureBlockedError) as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO work-plan stock master failed cold replay for {target.code}: {exc}"
            ) from exc
        org_id = str(plan["org_id"])
        if (
            len(raw) != evidence.byte_count
            or evidence.source_id != "CNINFO_STOCK_MASTER"
            or evidence.role != "STOCK_MASTER"
            or evidence.source_url != cninfo.CNINFO_STOCK_MASTER_URL
            or evidence.method != "GET"
            or evidence.content_type != "application/json"
            or str(master_rows.get(target.code[:6], {}).get("orgId") or "") != org_id
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO work-plan stock-master binding mismatch for {target.code}"
            )
        self._replay_page(
            target,
            page_value=dict(plan["first_page"]),
            org_id=org_id,
            page_count=int(plan["page_count"]),
            total=int(plan["document_count"]),
        )
        return (
            dict(plan["stock_master"]),
            org_id,
            int(plan["page_count"]),
            int(plan["document_count"]),
        )

    def _write_page_checkpoint(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        page_num: int,
        page_value: Mapping[str, Any],
        work_plan_sha256: str,
    ) -> None:
        payload = {
            "protocol_version": PAGE_CHECKPOINT_PROTOCOL_VERSION,
            "master_snapshot_id": self.scope.master_snapshot_id,
            "master_scope_sha256": self.scope.scope_sha256,
            "target": target.to_dict(),
            "work_plan_sha256": work_plan_sha256,
            "page_num": page_num,
            "page": dict(page_value),
            "caller_ready_accepted": False,
        }
        cninfo._atomic_write_exact(
            self.checkpoint_root,
            self._work_directory(target) / "pages" / f"{page_num:04d}.json",
            _canonical_json_bytes(payload),
        )

    def _load_page_checkpoint(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        org_id: str,
        page_num: int,
        page_count: int,
        total: int,
        work_plan_sha256: str,
        required: bool,
    ) -> dict[str, Any] | None:
        path = self._work_directory(target) / "pages" / f"{page_num:04d}.json"
        value = self._read_json_checkpoint(path, required=required)
        if value is None:
            return None
        expected = {
            "protocol_version",
            "master_snapshot_id",
            "master_scope_sha256",
            "target",
            "work_plan_sha256",
            "page_num",
            "page",
            "caller_ready_accepted",
        }
        if (
            set(value) != expected
            or value.get("protocol_version") != PAGE_CHECKPOINT_PROTOCOL_VERSION
            or value.get("master_snapshot_id") != self.scope.master_snapshot_id
            or value.get("master_scope_sha256") != self.scope.scope_sha256
            or value.get("target") != target.to_dict()
            or value.get("work_plan_sha256") != work_plan_sha256
            or value.get("page_num") != page_num
            or value.get("caller_ready_accepted") is not False
            or not isinstance(value.get("page"), dict)
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO page-checkpoint identity mismatch for {target.code}/{page_num}"
            )
        page_value = dict(value["page"])
        self._replay_page(
            target,
            page_value=page_value,
            org_id=org_id,
            page_count=page_count,
            total=total,
        )
        return page_value

    def _replay_page(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        page_value: Mapping[str, Any],
        org_id: str,
        page_count: int,
        total: int,
    ) -> tuple[dict[str, Any], ...]:
        expected_fields = {
            "exchange",
            "code",
            "org_id",
            "query_start",
            "query_end",
            "page_num",
            "page_size",
            "request",
            "raw",
        }
        if not isinstance(page_value, Mapping) or set(page_value) != expected_fields:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO page-checkpoint schema drift for {target.code}"
            )
        page_num = page_value.get("page_num")
        if type(page_num) is not int:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO page-checkpoint number is invalid for {target.code}"
            )
        expected_request = cninfo._announcement_request(target, org_id, page_num)
        if (
            page_value.get("exchange") != target.exchange
            or page_value.get("code") != target.code
            or page_value.get("org_id") != org_id
            or page_value.get("query_start") != target.query_start
            or page_value.get("query_end") != target.query_end
            or page_value.get("page_size") != cninfo.PAGE_SIZE
            or page_value.get("request") != expected_request
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO page-checkpoint scope mismatch for {target.code}/{page_num}"
            )
        try:
            evidence = cninfo._raw_from_mapping(page_value["raw"])
            raw, _ = self.cas.read_blob(
                evidence.content_hash,
                expected_path=evidence.object_path,
            )
            summary, rows = cninfo._parse_announcement_page(
                raw,
                target=target,
                org_id=org_id,
            )
        except cninfo.CninfoDelistedDisclosureBlockedError as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO page failed cold replay for {target.code}/{page_num}: {exc}"
            ) from exc
        if (
            len(raw) != evidence.byte_count
            or evidence.source_id
            != f"CNINFO_ANNOUNCEMENTS_{target.code}_{page_num}"
            or evidence.role != "ANNOUNCEMENT_PAGE"
            or evidence.source_url != cninfo.CNINFO_ANNOUNCEMENT_URL
            or evidence.method != "POST"
            or evidence.content_type != "application/json"
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO page raw identity mismatch for {target.code}/{page_num}"
            )
        self._validate_page_summary(
            summary,
            page_num=page_num,
            page_count=page_count,
            total=total,
        )
        return rows

    @staticmethod
    def _validate_page_summary(
        summary: Mapping[str, Any],
        *,
        page_num: int,
        page_count: int,
        total: int,
    ) -> None:
        expected_rows = (
            cninfo.PAGE_SIZE
            if page_num < page_count
            else total - cninfo.PAGE_SIZE * (page_count - 1)
        )
        if (
            int(summary["total"]) != total
            or int(summary["reported_totalpages"]) != page_count - 1
            or summary["has_more"] is not (page_num < page_count)
            or int(summary["row_count"]) != expected_rows
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                "CNINFO page disagrees with the frozen pagination plan"
            )

    def _write_document_checkpoint(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        document: Mapping[str, Any],
        work_plan_sha256: str,
    ) -> None:
        announcement_id = str(document.get("announcement_id") or "")
        payload = {
            "protocol_version": DOCUMENT_CHECKPOINT_PROTOCOL_VERSION,
            "master_snapshot_id": self.scope.master_snapshot_id,
            "master_scope_sha256": self.scope.scope_sha256,
            "target": target.to_dict(),
            "work_plan_sha256": work_plan_sha256,
            "announcement_id": announcement_id,
            "document": dict(document),
            "caller_ready_accepted": False,
        }
        cninfo._atomic_write_exact(
            self.checkpoint_root,
            self._work_directory(target)
            / "documents"
            / f"{announcement_id}.json",
            _canonical_json_bytes(payload),
        )

    def _load_document_checkpoint(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        row: Mapping[str, Any],
        work_plan_sha256: str,
        required: bool,
    ) -> dict[str, Any] | None:
        announcement_id = str(row["announcementId"])
        path = (
            self._work_directory(target)
            / "documents"
            / f"{announcement_id}.json"
        )
        value = self._read_json_checkpoint(path, required=required)
        if value is None:
            return None
        expected = {
            "protocol_version",
            "master_snapshot_id",
            "master_scope_sha256",
            "target",
            "work_plan_sha256",
            "announcement_id",
            "document",
            "caller_ready_accepted",
        }
        if (
            set(value) != expected
            or value.get("protocol_version")
            != DOCUMENT_CHECKPOINT_PROTOCOL_VERSION
            or value.get("master_snapshot_id") != self.scope.master_snapshot_id
            or value.get("master_scope_sha256") != self.scope.scope_sha256
            or value.get("target") != target.to_dict()
            or value.get("work_plan_sha256") != work_plan_sha256
            or value.get("announcement_id") != announcement_id
            or value.get("caller_ready_accepted") is not False
            or not isinstance(value.get("document"), dict)
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO document-checkpoint identity mismatch for {announcement_id}"
            )
        document = dict(value["document"])
        expected_url = cninfo._normalize_pdf_url(
            row["adjunctUrl"], announcement_id
        )
        try:
            evidence = cninfo._raw_from_mapping(document["raw"])
            raw, _ = self.cas.read_blob(
                evidence.content_hash,
                expected_path=evidence.object_path,
            )
        except (KeyError, cninfo.CninfoDelistedDisclosureBlockedError) as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO document failed cold replay for {announcement_id}: {exc}"
            ) from exc
        if (
            set(document) != {"exchange", "code", "announcement_id", "raw"}
            or document["exchange"] != target.exchange
            or document["code"] != target.code
            or document["announcement_id"] != announcement_id
            or evidence.source_id != f"CNINFO_PDF_{announcement_id}"
            or evidence.role != "SOURCE_DOCUMENT"
            or evidence.source_url != expected_url
            or evidence.method != "GET"
            or evidence.content_type != "application/pdf"
            or len(raw) != evidence.byte_count
            or len(raw) > cninfo.MAX_PDF_BYTES
            or not raw.startswith(b"%PDF-")
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO document raw identity mismatch for {announcement_id}"
            )
        return document

    def _parse_document(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        row: Mapping[str, Any],
        document: Mapping[str, Any],
    ) -> dict[str, Any]:
        evidence = cninfo._raw_from_mapping(document["raw"])
        raw, _ = self.cas.read_blob(
            evidence.content_hash,
            expected_path=evidence.object_path,
        )
        normalized = cninfo._normalized_announcement_base(
            target=target,
            row=row,
            evidence=evidence,
        )
        return cninfo._build_pdf_parse_evidence(
            announcement_row=row,
            normalized_announcement=normalized,
            raw_content_sha256=evidence.content_hash,
            pdf_raw=raw,
        ).to_dict()

    def _write_parse_checkpoint(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        announcement_id: str,
        parse_evidence: Mapping[str, Any],
        work_plan_sha256: str,
    ) -> None:
        payload = {
            "protocol_version": PARSE_CHECKPOINT_PROTOCOL_VERSION,
            "master_snapshot_id": self.scope.master_snapshot_id,
            "master_scope_sha256": self.scope.scope_sha256,
            "target": target.to_dict(),
            "work_plan_sha256": work_plan_sha256,
            "announcement_id": announcement_id,
            "parse_evidence": dict(parse_evidence),
            "caller_ready_accepted": False,
        }
        cninfo._atomic_write_exact(
            self.checkpoint_root,
            self._work_directory(target)
            / "parses"
            / f"{announcement_id}.json",
            _canonical_json_bytes(payload),
        )

    def _load_parse_checkpoint(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        row: Mapping[str, Any],
        document: Mapping[str, Any],
        work_plan_sha256: str,
        required: bool,
    ) -> dict[str, Any] | None:
        announcement_id = str(row["announcementId"])
        path = (
            self._work_directory(target)
            / "parses"
            / f"{announcement_id}.json"
        )
        value = self._read_json_checkpoint(path, required=required)
        if value is None:
            return None
        expected = {
            "protocol_version",
            "master_snapshot_id",
            "master_scope_sha256",
            "target",
            "work_plan_sha256",
            "announcement_id",
            "parse_evidence",
            "caller_ready_accepted",
        }
        if (
            set(value) != expected
            or value.get("protocol_version") != PARSE_CHECKPOINT_PROTOCOL_VERSION
            or value.get("master_snapshot_id") != self.scope.master_snapshot_id
            or value.get("master_scope_sha256") != self.scope.scope_sha256
            or value.get("target") != target.to_dict()
            or value.get("work_plan_sha256") != work_plan_sha256
            or value.get("announcement_id") != announcement_id
            or value.get("caller_ready_accepted") is not False
            or not isinstance(value.get("parse_evidence"), dict)
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO parse-checkpoint identity mismatch for {announcement_id}"
            )
        try:
            evidence = cninfo._raw_from_mapping(document["raw"])
            normalized = cninfo._normalized_announcement_base(
                target=target,
                row=row,
                evidence=evidence,
            )
            parsed = cninfo._validate_pdf_parse_evidence(
                value["parse_evidence"],
                announcement_row=row,
                normalized_announcement=normalized,
                raw_content_sha256=evidence.content_hash,
            )
        except cninfo.CninfoDelistedDisclosureBlockedError as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO parse evidence failed cold replay for {announcement_id}: {exc}"
            ) from exc
        return parsed.to_dict()

    def _read_json_checkpoint(
        self,
        path: Path,
        *,
        required: bool,
    ) -> dict[str, Any] | None:
        if not path.exists():
            if required:
                raise CninfoAnnouncementCaptureBlockedError(
                    f"CNINFO checkpoint is missing: {path.name}"
                )
            return None
        try:
            raw = cninfo._stable_read(self.checkpoint_root, path)
            value = json.loads(raw.decode("utf-8"))
        except (
            cninfo.CninfoDelistedDisclosureBlockedError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO checkpoint cannot be cold replayed: {path.name}: {exc}"
            ) from exc
        if not isinstance(value, dict) or _canonical_json_bytes(value) != raw:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO checkpoint is not canonical: {path.name}"
            )
        return value

    def _write_target_checkpoint(
        self,
        target: cninfo.FrozenDisclosureTarget,
        reference: cninfo.CninfoDelistedDisclosureManifestReference,
    ) -> None:
        payload = {
            "protocol_version": CHECKPOINT_PROTOCOL_VERSION,
            "master_snapshot_id": self.scope.master_snapshot_id,
            "master_scope_sha256": self.scope.scope_sha256,
            "target": target.to_dict(),
            "manifest_sha256": reference.manifest_sha256,
            "manifest_byte_count": reference.byte_count,
            "manifest_cas_uri": reference.cas_uri,
            "manifest_object_path": reference.object_path,
            "caller_ready_accepted": False,
        }
        content = _canonical_json_bytes(payload)
        directory = self._checkpoint_directory(target)
        path = directory / "current.json"
        cninfo._atomic_write_exact(self.checkpoint_root, path, content)

    def _load_target_checkpoint(
        self,
        target: cninfo.FrozenDisclosureTarget,
        *,
        required: bool,
    ) -> tuple[str, cninfo.CninfoDelistedDisclosureArtifact] | None:
        directory = self._checkpoint_directory(target)
        if not directory.exists():
            if required:
                raise CninfoAnnouncementCaptureBlockedError(
                    f"CNINFO checkpoint is missing for {target.code}"
                )
            return None
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO checkpoint directory cannot be read: {target.code}"
            ) from exc
        names = {item.name for item in entries}
        if names == {"_work"} and (directory / "_work").is_dir():
            if required:
                raise CninfoAnnouncementCaptureBlockedError(
                    f"CNINFO checkpoint is missing for {target.code}"
                )
            return None
        if (
            names not in ({"current.json"}, {"current.json", "_work"})
            or not (directory / "current.json").is_file()
            or ("_work" in names and not (directory / "_work").is_dir())
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO checkpoint is ambiguous for {target.code}"
            )
        path = directory / "current.json"
        try:
            raw = cninfo._stable_read(self.checkpoint_root, path)
            value = json.loads(raw.decode("utf-8"))
        except (
            cninfo.CninfoDelistedDisclosureBlockedError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO checkpoint cannot be replayed for {target.code}: {exc}"
            ) from exc
        expected_fields = {
            "protocol_version",
            "master_snapshot_id",
            "master_scope_sha256",
            "target",
            "manifest_sha256",
            "manifest_byte_count",
            "manifest_cas_uri",
            "manifest_object_path",
            "caller_ready_accepted",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected_fields
            or _canonical_json_bytes(value) != raw
            or value.get("protocol_version") != CHECKPOINT_PROTOCOL_VERSION
            or value.get("master_snapshot_id")
            != self.scope.master_snapshot_id
            or value.get("master_scope_sha256") != self.scope.scope_sha256
            or value.get("target") != target.to_dict()
            or value.get("caller_ready_accepted") is not False
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO checkpoint identity mismatch for {target.code}"
            )
        manifest_hash = _sha256_identity(
            value.get("manifest_sha256"), "CNINFO checkpoint manifest"
        )
        if value.get("manifest_cas_uri") != f"sha256:{manifest_hash}":
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO checkpoint manifest URI mismatch for {target.code}"
            )
        try:
            manifest_bytes, manifest_path = self.cas.read_blob(manifest_hash)
            artifact = self.manifest_store.replay(manifest_hash)
        except cninfo.CninfoDelistedDisclosureBlockedError as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO target manifest failed cold replay for {target.code}: {exc}"
            ) from exc
        if (
            type(value.get("manifest_byte_count")) is not int
            or value["manifest_byte_count"] != len(manifest_bytes)
            or Path(str(value.get("manifest_object_path") or "")).resolve()
            != manifest_path.resolve()
            or artifact.master_snapshot_id != self.scope.master_snapshot_id
            or artifact.targets != (target,)
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                f"CNINFO target manifest scope mismatch for {target.code}"
            )
        return manifest_hash, artifact

    def _rebuild_aggregate(
        self,
        targets: tuple[cninfo.FrozenDisclosureTarget, ...],
        artifacts: Sequence[cninfo.CninfoDelistedDisclosureArtifact],
        *,
        target_cas: cninfo.CninfoDisclosureCAS,
    ) -> tuple[cninfo.CninfoDelistedDisclosureArtifact, int]:
        if len(targets) != len(artifacts) or not artifacts:
            raise CninfoAnnouncementCaptureBlockedError(
                "announcement aggregate input is incomplete"
            )
        copied_hashes: set[str] = set()
        query_pages: list[dict[str, Any]] = []
        for artifact in artifacts:
            for page in artifact.query_pages:
                value = dict(page)
                value["raw"] = self._copy_raw_evidence(
                    page["raw"], target_cas, copied_hashes
                )
                query_pages.append(value)
        documents: list[dict[str, Any]] = []
        for artifact in artifacts:
            for document in artifact.documents:
                value = dict(document)
                value["raw"] = self._copy_raw_evidence(
                    document["raw"], target_cas, copied_hashes
                )
                documents.append(value)
        stock_masters = sorted(
            {artifact.stock_master for artifact in artifacts},
            key=lambda item: (item.retrieved_at, item.content_hash),
            reverse=True,
        )
        last_error: Exception | None = None
        for stock_master in stock_masters:
            try:
                copied_stock_master = self._copy_raw_evidence(
                    stock_master.to_dict(), target_cas, copied_hashes
                )
                aggregate = cninfo._rebuild_artifact(
                    cas=target_cas,
                    master_snapshot_id=self.scope.master_snapshot_id,
                    targets=targets,
                    stock_master=copied_stock_master,
                    query_pages=query_pages,
                    documents=documents,
                )
                return aggregate, len(copied_hashes)
            except cninfo.CninfoDelistedDisclosureBlockedError as exc:
                last_error = exc
        raise CninfoAnnouncementCaptureBlockedError(
            "no cold-replayed CNINFO stock master supports the aggregate scope: "
            f"{last_error}"
        )

    def _copy_raw_evidence(
        self,
        value: Mapping[str, Any],
        target_cas: cninfo.CninfoDisclosureCAS,
        copied_hashes: set[str],
    ) -> dict[str, Any]:
        evidence = cninfo._raw_from_mapping(value)
        raw, _source_path = self.cas.read_blob(
            evidence.content_hash,
            expected_path=evidence.object_path,
        )
        if len(raw) != evidence.byte_count:
            raise CninfoAnnouncementCaptureBlockedError(
                f"raw evidence byte count changed: {evidence.source_id}"
            )
        copied_hash, copied_path = target_cas.put_blob(raw)
        if copied_hash != evidence.content_hash:
            raise CninfoAnnouncementCaptureBlockedError(
                f"raw evidence hash changed during CAS copy: {evidence.source_id}"
            )
        copied_hashes.add(copied_hash)
        return {
            **evidence.to_dict(),
            "object_path": str(copied_path),
        }


def _parse_master_jsonl(raw: bytes) -> tuple[SecurityMasterRecord, ...]:
    if not raw or not raw.endswith(b"\n"):
        raise CninfoAnnouncementCaptureBlockedError(
            "security-master JSONL is empty or unterminated"
        )
    expected_fields = {
        "canonical_entity_id",
        "exchange",
        "code_alias",
        "board",
        "listed_at",
        "delisted_at",
        "valid_from",
        "valid_to",
        "event_type",
        "source_url",
        "source_hash",
        "retrieved_at",
        "name",
        "attributes",
    }
    records: list[SecurityMasterRecord] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CninfoAnnouncementCaptureBlockedError(
                "security-master JSONL contains invalid JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or set(value) != expected_fields
            or _canonical_json_bytes(value) != line
            or not isinstance(value.get("attributes"), dict)
        ):
            raise CninfoAnnouncementCaptureBlockedError(
                "security-master JSONL row schema drift"
            )
        records.append(SecurityMasterRecord(**value))
    return tuple(records)


def _derive_szse_targets(
    records: Sequence[SecurityMasterRecord],
) -> tuple[cninfo.FrozenDisclosureTarget, ...]:
    targets: list[cninfo.FrozenDisclosureTarget] = []
    seen_codes: set[str] = set()
    for record in records:
        if record.exchange != "SZSE" or record.delisted_at is None:
            continue
        listed = date.fromisoformat(record.listed_at)
        valid_from = date.fromisoformat(record.valid_from)
        delisted = date.fromisoformat(record.delisted_at)
        valid_to = date.fromisoformat(record.valid_to) if record.valid_to else delisted
        start = max(AUDIT_START, listed, valid_from)
        end_exclusive = min(AUDIT_END_EXCLUSIVE, delisted, valid_to)
        if start >= end_exclusive:
            continue
        if record.code_alias in seen_codes:
            raise CninfoAnnouncementCaptureBlockedError(
                f"duplicate SZSE target interval: {record.code_alias}"
            )
        seen_codes.add(record.code_alias)
        targets.append(
            cninfo.FrozenDisclosureTarget(
                canonical_entity_id=record.canonical_entity_id,
                exchange="SZSE",
                code=record.code_alias,
                query_start=start.isoformat(),
                query_end=(end_exclusive - timedelta(days=1)).isoformat(),
            )
        )
    return tuple(sorted(targets, key=lambda item: item.code))


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_identity(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise CninfoAnnouncementCaptureBlockedError(
            f"{label} is not a canonical SHA-256 digest"
        )
    return digest


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


__all__ = [
    "AnnouncementCaptureProgress",
    "AnnouncementIndexMaterialization",
    "AuthoritativeAnnouncementScope",
    "CHECKPOINT_PROTOCOL_VERSION",
    "CninfoAnnouncementCaptureBlockedError",
    "CninfoAnnouncementCaptureCoordinator",
    "EXPECTED_SZSE_TARGET_COUNT",
    "PROTOCOL_VERSION",
    "load_authoritative_szse_announcement_scope",
]
