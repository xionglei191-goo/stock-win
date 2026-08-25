from __future__ import annotations

import html
import io
import json
import os
import re
import shutil
import stat
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

import pandas as pd
import requests

from .hashing import canonical_json_bytes, sha256_bytes, sha256_file, sha256_json
from .models import LicenseClass, SourceDependency, SourceRole
from .sources_official import _require_sec_user_agent
from .store import SourceBatch, USPITStore


DIRECT_ACTION_REVIEW_VERSION = "us-pit-direct-action-evidence-v1"
DIRECT_ACTION_SOURCE_VERSION = "official-action-review-source-v1"
EVIDENCE_STATUSES = {
    "TERMS_COMPLETE_MODEL_GAP",
    "TERMS_COMPLETE_READY_FOR_FORMAL_REVIEW",
    "TERMS_PARTIAL",
    "SOURCE_GAP",
}
_OFFICIAL_HOST_SUFFIXES = (
    "sec.gov",
    "bbwinc.com",
    "howmet.com",
    "q4cdn.com",
    "rtx.com",
    "tranetechnologies.com",
)

_Transport = Callable[[str, str], tuple[bytes, str]]


@dataclass(frozen=True)
class DirectActionEvidenceResult:
    path: Path
    manifest: Mapping[str, Any]
    source_batch: SourceBatch | None


def _default_transport(url: str, user_agent: str) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*",
                    "Accept-Encoding": "identity",
                },
                timeout=45,
                allow_redirects=True,
            )
            response.raise_for_status()
            media_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
            return response.content, media_type
        except requests.RequestException as exc:  # pragma: no cover - live sources only
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise RuntimeError(f"failed to freeze official action evidence: {url}") from last_error


def _plain_text(payload: bytes, media_type: str) -> str:
    if media_type == "application/pdf" or payload.startswith(b"%PDF"):
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - dependency is present in production
            raise ValueError("pypdf is required to verify official PDF evidence") from exc
        value = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages)
    else:
        value = payload.decode("utf-8", errors="replace")
        value = re.sub(
            r"<script\b.*?</script>|<style\b.*?</style>",
            " ",
            value,
            flags=re.I | re.S,
        )
        value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _official_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _OFFICIAL_HOST_SUFFIXES
    )


def _mark_read_only(path: Path) -> None:
    path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


class DirectActionEvidenceReviewService:
    """Freeze and verify a reviewer-authored official evidence matrix.

    This is deliberately a candidate-only review product. It cannot set
    ``human_terms_reviewed`` or produce release-ready corporate actions.
    """

    def __init__(
        self,
        store: USPITStore | Path | str,
        *,
        user_agent: str | None = None,
        transport: _Transport | None = None,
        throttle_seconds: float = 0.11,
    ) -> None:
        self.store = store if isinstance(store, USPITStore) else USPITStore(store)
        self.user_agent = _require_sec_user_agent(user_agent)
        self.transport = transport or _default_transport
        self.throttle_seconds = max(0.0, float(throttle_seconds))

    def review(
        self,
        blocked_reviews: Path | str,
        review_spec: Path | str,
        output_dir: Path | str,
    ) -> DirectActionEvidenceResult:
        blocked_path = Path(blocked_reviews)
        spec_path = Path(review_spec)
        target = Path(output_dir)
        blocked = pd.read_parquet(blocked_path)
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        entries = spec.get("events")
        if spec.get("format_version") != DIRECT_ACTION_REVIEW_VERSION:
            raise ValueError("direct action review spec version is unsupported")
        if not isinstance(entries, list) or not entries:
            raise ValueError("direct action review spec requires events")
        blocked_ids = set(blocked["request_id"].astype(str))
        entry_ids = {str(item.get("request_id") or "") for item in entries}
        if "" in entry_ids or blocked_ids != entry_ids or len(entry_ids) != len(entries):
            raise ValueError("direct action review must cover each blocked request exactly once")

        observed_at = datetime.now(timezone.utc).isoformat()
        dependencies: list[SourceDependency] = []
        rows: list[dict[str, Any]] = []
        blocked_by_id = blocked.set_index("request_id")
        for entry in sorted(entries, key=lambda item: str(item["request_id"])):
            request_id = str(entry["request_id"])
            status = str(entry.get("evidence_status") or "")
            if status not in EVIDENCE_STATUSES:
                raise ValueError(f"invalid evidence status for {request_id}")
            sources = entry.get("sources")
            if not isinstance(sources, list) or not sources:
                raise ValueError(f"review event has no official sources: {request_id}")
            source_hashes: list[str] = []
            source_urls: list[str] = []
            source_records: list[dict[str, Any]] = []
            all_phrases_verified = True
            for source in sources:
                url = str(source.get("url") or "")
                if not _official_url(url):
                    raise ValueError(f"non-official action review URL: {url}")
                capture_gap = str(source.get("capture_gap") or "").strip()
                if capture_gap:
                    all_phrases_verified = False
                    source_urls.append(url)
                    source_records.append({
                        "url": url,
                        "object_sha256": "",
                        "published_at": str(source.get("published_at") or ""),
                        "required_phrases_verified": False,
                        "phrase_results": {},
                        "capture_gap": capture_gap,
                    })
                    continue
                existing = str(source.get("object_sha256") or "")
                if existing:
                    object_path = self.store.object_path(existing)
                    if not object_path.is_file() or sha256_file(object_path) != existing:
                        raise ValueError(f"frozen action evidence is missing or corrupt: {existing}")
                    payload = object_path.read_bytes()
                    media_type = str(source.get("media_type") or "text/plain")
                else:
                    try:
                        payload, media_type = self.transport(url, self.user_agent)
                    except Exception as exc:
                        all_phrases_verified = False
                        source_urls.append(url)
                        source_records.append({
                            "url": url,
                            "object_sha256": "",
                            "published_at": str(source.get("published_at") or ""),
                            "required_phrases_verified": False,
                            "phrase_results": {},
                            "capture_gap": f"{type(exc).__name__}: source capture failed",
                        })
                        continue
                    finally:
                        if self.throttle_seconds:
                            time.sleep(self.throttle_seconds)
                    reference = self.store.put_bytes(payload, media_type=media_type)
                    existing = reference.sha256
                text = _plain_text(payload, media_type)
                required_phrases = tuple(
                    str(value).strip() for value in source.get("required_phrases", []) if str(value).strip()
                )
                phrase_results = {
                    phrase: phrase.casefold() in text.casefold()
                    for phrase in required_phrases
                }
                phrases_verified = bool(required_phrases) and all(phrase_results.values())
                all_phrases_verified = all_phrases_verified and phrases_verified
                published_at = str(source.get("published_at") or "")
                timestamp = pd.Timestamp(published_at)
                if timestamp.tzinfo is None:
                    raise ValueError(f"official source published_at must be timezone-aware: {url}")
                source_id = str(source.get("source_id") or "official_issuer_action_evidence")
                dependency = SourceDependency(
                    source_id=source_id,
                    source_version=DIRECT_ACTION_SOURCE_VERSION,
                    role=SourceRole.VALIDATION_ANCHOR,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    object_sha256=existing,
                    observed_at=observed_at,
                    published_at=timestamp.isoformat(),
                    as_of_date=str(blocked_by_id.loc[request_id, "anchor_date"]),
                    url=url,
                    dataset="corporate_action_direct_review_source",
                    metadata={
                        "eligible_for_historical_signal": False,
                        "candidate_only": True,
                        "human_terms_reviewed": False,
                        "request_id": request_id,
                        "required_phrase_sha256": sha256_json(list(required_phrases)),
                        "required_phrases_verified": phrases_verified,
                    },
                )
                dependencies.append(dependency)
                source_hashes.append(existing)
                source_urls.append(url)
                source_records.append({
                    "url": url,
                    "object_sha256": existing,
                    "published_at": timestamp.isoformat(),
                    "required_phrases_verified": phrases_verified,
                    "phrase_results": phrase_results,
                })
            if status.startswith("TERMS_COMPLETE") and not all_phrases_verified:
                status = "TERMS_PARTIAL"
            blocked_row = blocked_by_id.loc[request_id]
            rows.append({
                "request_id": request_id,
                "anchor_date": str(blocked_row["anchor_date"]),
                "predecessor_security_id": str(blocked_row["predecessor_security_id"]),
                "successor_security_id": str(blocked_row["successor_security_id"]),
                "predecessor_name": str(blocked_row["predecessor_name"]),
                "successor_name": str(blocked_row["successor_name"]),
                "evidence_status": status,
                "terms_complete": status.startswith("TERMS_COMPLETE"),
                "model_blocker": str(entry.get("model_blocker") or ""),
                "review_conclusion": str(entry.get("review_conclusion") or ""),
                "action_legs_json": canonical_json_bytes(entry.get("action_legs") or []).decode("utf-8"),
                "source_records_json": canonical_json_bytes(source_records).decode("utf-8"),
                "source_hashes_json": canonical_json_bytes(sorted(set(source_hashes))).decode("utf-8"),
                "source_urls_json": canonical_json_bytes(source_urls).decode("utf-8"),
                "all_required_phrases_verified": all_phrases_verified,
                "reviewer": str(spec.get("reviewer") or "codex-official-evidence-review"),
                "human_approval_claimed": False,
                "formal_action_rows_emitted": False,
            })
        frame = pd.DataFrame(rows)
        source_batch = self.store.write_source_batch(dependencies) if dependencies else None
        stage = target.with_name(f".{target.name}.{uuid4().hex}.staging")
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        matrix_path = stage / "action_evidence_matrix.parquet"
        frame.to_parquet(matrix_path, index=False)
        manifest = {
            "format_version": DIRECT_ACTION_REVIEW_VERSION,
            "review_id": sha256_json({
                "input_sha256": {
                    "blocked_reviews": sha256_file(blocked_path),
                    "review_spec": sha256_file(spec_path),
                },
                "matrix_sha256": sha256_file(matrix_path),
                "source_batch_id": None if source_batch is None else source_batch.batch_id,
            }),
            "created_at": observed_at,
            "status": "REVIEW_REQUIRED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "human_approval_claimed": False,
            "formal_action_rows_emitted": False,
            "input_sha256": {
                "blocked_reviews": sha256_file(blocked_path),
                "review_spec": sha256_file(spec_path),
            },
            "artifacts": {"action_evidence_matrix.parquet": sha256_file(matrix_path)},
            "source_batch_id": None if source_batch is None else source_batch.batch_id,
            "event_count": len(frame),
            "terms_complete_count": int(frame["terms_complete"].sum()),
            "model_gap_count": int(frame["evidence_status"].eq("TERMS_COMPLETE_MODEL_GAP").sum()),
            "source_gap_count": int(frame["evidence_status"].eq("SOURCE_GAP").sum()),
            "terms_partial_count": int(frame["evidence_status"].eq("TERMS_PARTIAL").sum()),
        }
        manifest_path = stage / "manifest.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("review_id") == manifest["review_id"]:
                shutil.rmtree(stage)
                return DirectActionEvidenceResult(target, existing, source_batch)
            raise ValueError(f"direct action evidence output already exists: {target}")
        os.replace(stage, target)
        for path in target.iterdir():
            if path.is_file():
                _mark_read_only(path)
        return DirectActionEvidenceResult(target, manifest, source_batch)


__all__ = [
    "DIRECT_ACTION_REVIEW_VERSION",
    "DirectActionEvidenceResult",
    "DirectActionEvidenceReviewService",
]
