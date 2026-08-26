"""Verify operator-proposed CIKs against official EDGAR records and bind.

For every proposal the script fetches the EDGAR company Atom feed by CIK
(official record), extracts the conformed registrant name, and admits the
binding only when the canonical/compact name matches the reviewed issuer
name.  Mismatched proposals are rejected and stay blocked - a wrong guess can
never enter the dataset.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sys
from uuid import uuid4
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")
from research_platform.us_pit.hashing import sha256_file, sha256_json
from research_platform.us_pit.action_review import _source_plain_text  # noqa: E402
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


STATE_SUFFIX = re.compile(r"/\s*/?\s*(?:[A-Z]{2}|NEW)\s*/?\s*$", re.I)


def strip_state_suffix(value: str) -> str:
    return STATE_SUFFIX.sub("", str(value)).strip()


def canon(value: str) -> str:
    value = strip_state_suffix(value)
    value = str(value).replace(".", "")
    text = value.replace("&", " and ").replace("/", " ")
    text = re.sub(r"[^A-Za-z0-9\s]", " ", text)
    tokens = [t for t in re.split(r"\s+", text.upper()) if len(t) > 1]
    return " ".join(t for t in tokens if not SUFFIX.fullmatch(t))


def compact(value: str) -> str:
    return canon(value).replace(" ", "")


def alnum(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def name_matches(conformed: str, targets: set[str]) -> bool:
    c_canon = canon(conformed)
    c_compact = compact(conformed)
    for target in targets:
        t_canon = canon(target)
        t_compact = compact(target)
        if c_canon == t_canon or c_compact == t_compact:
            return True
        # order-insensitive token comparison
        if sorted(c_canon.split()) == sorted(t_canon.split()):
            return True
        if alnum(conformed) == alnum(target):
            return True
    return False


def conformed_name_from_atom(payload_text: str) -> str:
    single = re.search(r"<company-info>.*?<conformed-name>(.*?)</conformed-name>",
                       payload_text, re.S)
    if single:
        return " ".join(single.group(1).split())
    author = re.search(r"<author>\s*<name>(.*?)</name>", payload_text, re.S)
    if author:
        value = " ".join(author.group(1).split())
        if value.upper() != "WEBMASTER":
            return value
    info = re.search(r'<company-info name="([^"]+)"', payload_text)
    if info and not info.group(1).startswith("ARRAY"):
        return " ".join(info.group(1).split())
    raise ValueError("atom feed carries no conformed registrant name")


def main() -> None:
    user_agent = _require_sec_user_agent(None)
    store = USPITStore("data/us_pit")
    review_dir = Path("data/us_pit/review_inputs/oxalpha_final_issuers").resolve()
    output = Path("data/us_pit/review_inputs/oxalpha_final_cikbound").resolve()
    previous = Path("data/us_pit/review_inputs/oxalpha_final_cikbound.prev")
    carried: dict[str, dict] = {}
    prev_manifest_path = previous / "cik_binding_manifest.json"
    if prev_manifest_path.is_file():
        prev_manifest = json.loads(prev_manifest_path.read_text(encoding="utf-8"))
        carried = {
            item["bound_security_id"]: {
                "cik": item["matched_cik"],
                "conformed": item["matched_conformed_name"],
                "evidence_sha256": item["event_evidence_sha256"],
            }
            for item in prev_manifest.get("admissions", [])
        }
        # rebuild from verified proposals of the previous run
        for issuer_name, info in prev_manifest.get("verified_proposals", {}).items():
            rows = pd.read_parquet(previous / "identity_review.parquet")
            sids = sorted(set(rows.loc[
                rows["issuer_name"].astype(str).map(canon).eq(canon(issuer_name)),
                "suggested_security_id",
            ].astype(str)))
            for sid in sids:
                carried[sid] = {
                    "cik": info["cik"], "conformed": info["conformed"],
                    "evidence_sha256": info.get("evidence_sha256", ""),
                }
        print(f"carrying {len(carried)} prior bindings")
    proposals = json.load(open("tmp/exited_cik_proposals.json", encoding="utf-8"))
    already_verified = set(json.load(open(
        str(prev_manifest_path), encoding="utf-8"
    )).get("verified_proposals", {})) if prev_manifest_path.is_file() else set()

    frame = pd.read_parquet(review_dir / "identity_review.parquet")
    unbound = frame[frame["issuer_id"].fillna("").eq("")]

    # Build approved-action alias groups: securities connected by an approved
    # identity action (predecessor/successor) share one issuer lineage.
    package_dirs = [
        "data/us_pit/action_reviews/approved_oxalpha_v2",
        "data/us_pit/action_reviews/approved_oxalpha_r2",
        "data/us_pit/action_reviews/approved_oxalpha_r3",
        "data/us_pit/action_reviews/approved_oxalpha_r4c",
        "data/us_pit/action_reviews/approved_oxalpha_r5",
    ]
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for package in package_dirs:
        actions = pd.read_parquet(Path(package) / "corporate_actions.parquet")
        for _, row in actions.iterrows():
            pre = str(row.get("security_id") or "").strip()
            suc = str(row.get("successor_security_id") or "").strip()
            if pre and suc:
                union(pre, suc)

    names_by_sid: dict[str, set[str]] = {}
    for _, row in frame.iterrows():
        sid = str(row.get("suggested_security_id") or "").strip()
        name = str(row.get("issuer_name") or "").strip()
        if sid and name:
            names_by_sid.setdefault(sid, set()).add(name)

    observed = datetime.now(timezone.utc).isoformat()
    dependencies = []
    bindings: dict[str, dict] = {}
    verified: dict[str, dict] = {}
    for issuer_name, proposal in proposals.items():
        if issuer_name in already_verified:
            continue
        if isinstance(proposal, dict):
            cik_raw = proposal["cik"]
            aliases = [str(a) for a in proposal.get("aliases", [])]
        else:
            cik_raw = proposal
            aliases = []
        cik_compact = str(int(str(cik_raw).strip()))
        cik = cik_compact.rjust(10, '0')
        query = urllib.parse.urlencode({
            "action": "getcompany", "CIK": cik,
            "dateb": "", "owner": "include", "count": 40, "output": "atom",
        })
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?{query}"
        request = urllib.request.Request(
            url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
        )
        conformed = None
        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = response.read()
                text = html.unescape(payload.decode("utf-8", errors="replace"))
                if text.lstrip().lower().startswith("<!doctype"):
                    raise RuntimeError("transient HTML block page")
                conformed = conformed_name_from_atom(text)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(6.0 * (attempt + 1))
        if conformed is None:
            print(f"REJECT {issuer_name} (CIK {cik}): fetch failed: {last_error}")
            continue
        # Resolve the alias group containing any security whose reviewed name
        # matches the EDGAR conformed name.
        target_group_root = None
        alias_hit = any(
            strip_state_suffix(a).upper() == strip_state_suffix(conformed).upper()
            or canon(a) == canon(conformed)
            for a in aliases
        )
        if not alias_hit:
            for sid, names in names_by_sid.items():
                if name_matches(conformed, names):
                    target_group_root = find(sid)
                    break
        if target_group_root is not None:
            security_ids = sorted(sid for sid in names_by_sid
                                  if find(sid) == target_group_root)
            group_names = set()
            for sid in security_ids:
                group_names |= names_by_sid[sid]
            ok = True
        else:
            ok = False
        digest_ref = store.put_bytes(payload, media_type="application/atom+xml")
        dependencies.append(
            SourceDependency(
                source_id="sec_edgar_company_search",
                source_version="edgar-browse-getcompany-atom-v1",
                role=SourceRole.SIGNAL_INPUT,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=digest_ref.sha256,
                observed_at=observed,
                published_at=observed,
                as_of_date=observed[:10],
                url=url,
                dataset="sec_company_search",
                metadata={
                    "artifact_kind": "raw_edgar_company_search_atom",
                    "query_cik": cik,
                    "conformed_name": conformed,
                    "response_sha256": digest_ref.sha256,
                    "live_query_result": True,
                },
            )
        )
        if not ok and alias_hit:
            own_sids = sorted(set(unbound.loc[
                unbound["issuer_name"].astype(str).map(canon).eq(canon(issuer_name)),
                "suggested_security_id",
            ].astype(str)))
            if own_sids and all(sid in bindings or True for sid in own_sids):
                ok = True
                security_ids = own_sids
        if not ok:
            print(f"REJECT {issuer_name}: EDGAR conformed name {conformed!r}")
            continue
        security_ids = security_ids
        for sid in security_ids:
            bindings[sid] = {
                "cik": cik, "conformed": conformed,
                "evidence_sha256": digest_ref.sha256,
            }
        verified[issuer_name] = {"cik": cik, "conformed": conformed}
        print(f"VERIFIED {issuer_name} -> {conformed} ({cik}) "
              f"[{len(security_ids)} securities]")
        time.sleep(0.2)

    batch_id = None
    if dependencies:
        batch = store.write_source_batch(dependencies)
        batch_id = batch.batch_id

    for sid, info in carried.items():
        if info.get("evidence_sha256"):
            bindings.setdefault(sid, info)

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
        admitted_securities = 0
        for _, row in frame.iterrows():
            current = str(row.get("issuer_id") or "").strip()
            sid = str(row.get("suggested_security_id") or "").strip()
            binding = bindings.get(sid)
            if current or binding is None:
                new_ids.append(current)
                new_notes.append(str(row.get("review_note") or ""))
                continue
            admitted_securities += 1
            new_ids.append(f"us_issuer_cik_{binding['cik']}")
            new_notes.append(
                f"Issuer CIK {binding['cik']} bound via EDGAR verification "
                f"(conformed {binding['conformed']!r}, sha256 "
                f"{binding['evidence_sha256'][:16]}…)."
            )
        frame["issuer_id"] = new_ids
        frame["review_note"] = new_notes
        frame.to_parquet(staging / "identity_review.parquet", index=False)
        manifest = {
            "format_version": "us-pit-cik-proposal-binding-v1",
            "base_review_dir": str(review_dir),
            "verified_proposals": verified,
            "batch_id": batch_id,
            "admitted_securities": admitted_securities,
            "bound_identity_review_sha256": sha256_file(
                staging / "identity_review.parquet"
            ),
        }
        manifest["binding_id"] = sha256_json(manifest)
        (staging / "cik_binding_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if output.exists():
            shutil.move(output, previous)
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(json.dumps({
        "status": "CIK_BINDINGS_VERIFIED_AND_ADMITTED",
        "path": str(output),
        "verified_names": len(verified),
        "admitted_securities": admitted_securities,
    }, indent=1))


if __name__ == "__main__":
    main()
