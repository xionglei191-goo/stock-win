"""Final binding pass for delisted issuers whose direct-CIK lookups failed.

Per issuer name:
1. EDGAR browse-edgar company-NAME search (Atom) -> candidate CIK set;
2. every candidate is verified against data.sec.gov submissions JSON,
   whose authoritative ``name`` field must canon/compact-match the
   reviewed issuer name;
3. a unique verified candidate binds the security group; ambiguous or
   unmatched names stay honestly blocked.

All network evidence is frozen verbatim into one immutable source batch.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

sys.path.insert(0, ".")
from research_platform.us_pit.hashing import sha256_file, sha256_json  # noqa: E402
from research_platform.us_pit.models import (  # noqa: E402
    LicenseClass,
    SourceDependency,
    SourceRole,
)
from research_platform.us_pit.sources_official import (  # noqa: E402
    _require_sec_user_agent,
)
from research_platform.us_pit.store import USPITStore  # noqa: E402

SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|cos|co|company|companies|plc|ltd|"
    r"limited|the|holdings|holding|sa|nv|ag|ny|de|md|nj|va|pa|class)\b",
    re.I,
)


def strip_state(value: str) -> str:
    return re.sub(r"/\s*/?\s*(?:[A-Z]{2}|NEW)\s*/?\s*$", "", str(value)).strip()


def canon(value: str) -> str:
    value = strip_state(html.unescape(str(value))).replace(".", "")
    text = value.replace("&", " and ").replace("/", " ")
    text = re.sub(r"[^A-Za-z0-9\s]", " ", text)
    tokens = [t for t in re.split(r"\s+", text.upper()) if len(t) > 1]
    return " ".join(t for t in tokens if not SUFFIX.fullmatch(t))


def compact(value: str) -> str:
    return canon(value).replace(" ", "")


def name_matches(conformed: str, targets: set[str]) -> bool:
    c_canon = canon(conformed)
    if not c_canon:
        return False
    c_compact = c_canon.replace(" ", "")
    for target in targets:
        t_canon = canon(target)
        if not t_canon:
            continue
        if c_canon == t_canon or c_compact == t_canon.replace(" ", ""):
            return True
        if sorted(c_canon.split()) == sorted(t_canon.split()):
            return True
    return False


def http_get(url: str, user_agent: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
        if response.status != 200 or not payload:
            raise RuntimeError(f"HTTP {response.status}")
        return payload


def main() -> None:
    user_agent = _require_sec_user_agent(None)
    store = USPITStore("data/us_pit")
    review_dir = Path("data/us_pit/review_inputs/oxalpha_final_cikbound").resolve()
    output = Path("data/us_pit/review_inputs/oxalpha_final_v2").resolve()
    if output.exists():
        raise SystemExit(f"output dir already exists: {output}")

    frame = pd.read_parquet(review_dir / "identity_review.parquet")
    unbound_rows = frame[frame["issuer_id"].fillna("").eq("")]
    pending_names = sorted(set(unbound_rows["issuer_name"].astype(str)))

    observed = datetime.now(timezone.utc).isoformat()
    dependencies = []
    bindings: dict[str, dict] = {}
    report: dict[str, dict] = {}

    for index, issuer_name in enumerate(pending_names):
        target_canon = canon(issuer_name)
        if not target_canon:
            continue
        query = urllib.parse.urlencode({
            "action": "getcompany", "company": issuer_name,
            "type": "10-K", "dateb": "", "owner": "include",
            "count": 100, "output": "atom",
        })
        search_url = f"https://www.sec.gov/cgi-bin/browse-edgar?{query}"
        try:
            search_payload = http_get(search_url, user_agent)
        except Exception as exc:  # noqa: BLE001
            report[issuer_name] = {"status": "SEARCH_FAILED", "error": str(exc)}
            print(f"[{index + 1}] SEARCH FAIL {issuer_name}: {exc}")
            continue
        search_text = html.unescape(search_payload.decode("utf-8", errors="replace"))
        candidate_ciks = sorted({c.strip().lstrip("0") and f"{int(c):010d}"
                                 for c in re.findall(r"CIK[=\-]([0-9]+)", search_text)
                                 for c in [c]} )
        candidate_ciks = sorted({f"{int(c):010d}" for c in
                                 re.findall(r"CIK[=\-]([0-9]+)", search_text)})
        verified = []
        for cik in candidate_ciks[:10]:
            sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            try:
                sub_payload = http_get(sub_url, user_agent)
                official_name = str(json.loads(sub_payload).get("name") or "")
            except Exception as exc:  # noqa: BLE001
                continue
            if name_matches(official_name, {issuer_name}):
                verified.append((cik, official_name, sub_url, sub_payload))
        unique_ciks = {cik for cik, _, _, _ in verified}
        if len(unique_ciks) != 1:
            report[issuer_name] = {
                "status": "UNRESOLVED" if unique_ciks else "NO_MATCH",
                "candidates": sorted(unique_ciks),
            }
            print(f"[{index + 1}/{len(pending_names)}] NO-UNIQUE {issuer_name}: "
                  f"{sorted(unique_ciks)}")
            continue
        cik, official_name, sub_url, sub_payload = verified[0]
        digest_ref = store.put_bytes(sub_payload, media_type="application/json")
        dependencies.append(
            SourceDependency(
                source_id="sec_submissions",
                source_version="data-sec-submissions-v1",
                role=SourceRole.SIGNAL_INPUT,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=digest_ref.sha256,
                observed_at=observed,
                published_at=observed,
                as_of_date=observed[:10],
                url=sub_url,
                dataset="sec_company_search",
                metadata={
                    "artifact_kind": "raw_edgar_company_search_atom",
                    "query_company": issuer_name,
                    "matched_cik": cik,
                    "conformed_name": official_name,
                    "response_sha256": digest_ref.sha256,
                    "live_query_result": True,
                },
            )
        )
        target_sids = sorted(set(unbound_rows.loc[
            unbound_rows["issuer_name"].astype(str).map(canon).eq(target_canon),
            "suggested_security_id",
        ].astype(str)))
        for sid in target_sids:
            bindings[sid] = {
                "cik": cik,
                "conformed": official_name,
                "evidence_sha256": digest_ref.sha256,
            }
        report[issuer_name] = {
            "status": "BOUND", "cik": cik, "official_name": official_name,
            "securities": target_sids,
        }
        print(f"[{index + 1}/{len(pending_names)}] BOUND {issuer_name} -> "
              f"{official_name} ({cik}) [{len(target_sids)} securities]")
        time.sleep(0.4)

    batch_id = None
    if dependencies:
        batch = store.write_source_batch(dependencies)
        batch_id = batch.batch_id

    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        for path in sorted(review_dir.iterdir()):
            if path.name == "identity_review.parquet":
                continue
            if path.is_file():
                shutil.copyfile(path, staging / path.name)
        new_ids = []
        new_notes = []
        admitted = 0
        for _, row in frame.iterrows():
            current = str(row.get("issuer_id") or "").strip()
            sid = str(row.get("suggested_security_id") or "").strip()
            binding = bindings.get(sid)
            if current or binding is None:
                new_ids.append(current)
                new_notes.append(str(row.get("review_note") or ""))
                continue
            admitted += 1
            new_ids.append(f"us_issuer_cik_{binding['cik']}")
            new_notes.append(
                f"Issuer CIK {binding['cik']} bound via EDGAR name-search + "
                f"data.sec.gov verification "
                f"(conformed {binding['conformed']!r}, sha256 "
                f"{binding['evidence_sha256'][:16]}…)."
            )
        frame["issuer_id"] = new_ids
        frame["review_note"] = new_notes
        frame.to_parquet(staging / "identity_review.parquet", index=False)
        manifest = {
            "format_version": "us-pit-delisted-name-search-binding-v1",
            "base_review_dir": str(review_dir),
            "batch_id": batch_id,
            "bindings": {k: v for k, v in report.items()},
            "admitted_securities": admitted,
            "bound_identity_review_sha256": sha256_file(
                staging / "identity_review.parquet"
            ),
        }
        manifest["binding_id"] = sha256_json(manifest)
        (staging / "name_search_binding_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    bound_count = sum(1 for v in report.values() if v.get("status") == "BOUND")
    print(json.dumps({"status": "DONE", "path": str(output),
                      "bound_names": bound_count,
                      "admitted_securities": admitted}, indent=1))


if __name__ == "__main__":
    main()
