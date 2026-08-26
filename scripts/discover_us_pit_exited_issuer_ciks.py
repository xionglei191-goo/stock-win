"""Discover correct CIKs for remaining delisted issuers.

For every still-unbound issuer name, tries multiple EDGAR company-search
query variants, collects candidate (conformed_name, cik) pairs from both
top-level <company-info> blocks and per-entry blocks, verifies every
candidate against data.sec.gov submissions (authoritative ``name`` field),
and freezes ALL fetched evidence.  Only unique canon-matched survivors are
reported as bindable proposals.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
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


def alnum(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def matches(conformed: str, target: str) -> bool:
    c_canon, t_canon = canon(conformed), canon(target)
    if not c_canon or not t_canon:
        return False
    if c_canon == t_canon or compact(conformed) == compact(target):
        return True
    if sorted(c_canon.split()) == sorted(t_canon.split()):
        return True
    if alnum(conformed) == alnum(target):
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


def parse_candidates(atom_text: str) -> list[tuple[str, str]]:
    """(conformed_name, cik10) pairs from single/multi-match Atom feeds."""
    pairs: dict[str, str] = {}
    top_cik = re.search(r"<company-info>.*?<cik>([0-9]+)</cik>", atom_text, re.S)
    top_name = re.search(r"<company-info>.*?<conformed-name>(.*?)</conformed-name>",
                         atom_text, re.S)
    if top_cik and top_name:
        pairs[f"{int(top_cik.group(1)):010d}"] = " ".join(top_name.group(1).split())
    for chunk in atom_text.split("<entry")[1:]:
        cik_match = re.search(r"CIK[=\-]([0-9]+)", chunk)
        info_match = re.search(r'<company-info name="([^"]+)"', chunk)
        conf_match = re.search(r"<conformed-name>(.*?)</conformed-name>", chunk, re.S)
        author_match = re.search(r"<author>\s*<name>(.*?)</name>", chunk, re.S)
        conformed = ""
        if conf_match:
            conformed = conf_match.group(1)
        elif info_match and not info_match.group(1).startswith("ARRAY"):
            conformed = info_match.group(1)
        elif author_match:
            candidate = " ".join(author_match.group(1).split())
            if candidate.upper() != "WEBMASTER":
                conformed = candidate
        if not cik_match or not conformed:
            continue
        pairs[f"{int(cik_match.group(1)):010d}"] = " ".join(conformed.split())
    return sorted(pairs.items())


def main() -> None:
    user_agent = _require_sec_user_agent(None)
    store = USPITStore("data/us_pit")
    import pandas as pd
    frame = pd.read_parquet(
        Path("data/us_pit/review_inputs/oxalpha_final_cikbound.prev6")
        / "identity_review.parquet"
    )
    unbound_rows = frame[frame["issuer_id"].fillna("").eq("")]
    pending = sorted(set(unbound_rows["issuer_name"].astype(str)))
    observed = datetime.now(timezone.utc).isoformat()

    dependencies = []
    proposals: dict[str, dict] = {}
    report: dict[str, dict] = {}

    def freeze(payload: bytes, url: str, label: str, query_name: str):
        digest_ref = store.put_bytes(payload, media_type="text/plain")
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
                    "query_company": query_name,
                    "discovery_label": label,
                    "response_sha256": digest_ref.sha256,
                    "live_query_result": True,
                },
            )
        )
        return digest_ref.sha256

    for index, issuer_name in enumerate(pending):
        variants = [
            issuer_name,
            canon(issuer_name),
            " ".join(canon(issuer_name).split()[:2]),
            (canon(issuer_name).split() or [""])[0],
        ]
        seen_queries = set()
        all_candidates: dict[str, set[str]] = {}
        frozen_digests = []
        for variant in variants:
            variant = variant.strip()
            if not variant or variant in seen_queries:
                continue
            seen_queries.add(variant)
            query = urllib.parse.urlencode({
                "action": "getcompany", "company": variant,
                "type": "10-K", "dateb": "", "owner": "include",
                "count": 100, "output": "atom",
            })
            url = f"https://www.sec.gov/cgi-bin/browse-edgar?{query}"
            try:
                payload = http_get(url, user_agent)
                text = html.unescape(payload.decode("utf-8", errors="replace"))
                digest = freeze(payload, url, f"variant:{variant}", issuer_name)
                frozen_digests.append(digest)
                for cand_cik, cand_name in parse_candidates(text):
                    all_candidates.setdefault(cand_cik, set()).add(cand_name)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{index + 1}] variant {variant!r}: {exc}")
            time.sleep(0.5)
        verified = []
        for cik, names in all_candidates.items():
            sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            try:
                sub_payload = http_get(sub_url, user_agent)
                official = str(json.loads(sub_payload).get("name") or "")
            except Exception as exc:  # noqa: BLE001
                print(f"  [{index + 1}] CIK {cik} submissions unavailable: {exc}")
                continue
            if matches(official, issuer_name):
                verified.append((cik, official))
            time.sleep(0.4)
        unique = sorted({cik for cik, _ in verified})
        if len(unique) == 1:
            cik = unique[0]
            official = next(n for c, n in verified if c == cik)
            proposals_entry = {"cik": cik}
            proposals_entry_conformed = official
            proposals[issuer_name] = {
                "cik": cik,
                "verified_conformed_name": official,
            }
            report[issuer_name] = {"status": "FOUND", "cik": cik,
                                   "official_name": official}
            print(f"[{index + 1}/{len(pending)}] FOUND {issuer_name} -> "
                  f"{official} ({cik})")
        else:
            report[issuer_name] = {"status": "UNRESOLVED", "candidates": unique}
            print(f"[{index + 1}/{len(pending)}] UNRESOLVED {issuer_name}: "
                  f"{unique}")
        time.sleep(0.5)

    batch_id = None
    if dependencies:
        batch = store.write_source_batch(dependencies)
        batch_id = batch.batch_id
    json.dump(
        {"batch_id": batch_id, "proposals": proposals, "report": report},
        open("tmp/exited_discovery_result.json", "w", encoding="utf-8"),
        indent=1, ensure_ascii=False,
    )
    found = sum(1 for v in report.values() if v.get("status") == "FOUND")
    print(json.dumps({"found": found, "total": len(pending),
                      "batch_id": batch_id}, indent=1))


if __name__ == "__main__":
    main()
