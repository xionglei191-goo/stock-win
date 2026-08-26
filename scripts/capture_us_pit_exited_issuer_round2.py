"""Second-round EDGAR company-search capture with progressive query
shortening for names that returned no results in round one.

For every still-unbound issuer name the script tries, in order:
1. the raw review name;
2. the name without corporate-suffix tokens;
3. the first two canonical tokens;
4. the first canonical token.
The first response whose parsed candidate set contains an exact canonical
match wins; only that response is frozen for the binding stage.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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
    r"limited|the|holdings|holding|sa|nv|ag)\b",
    re.I,
)

ENTRY_CIK = re.compile(r"CIK=([0-9]+)")
AUTHOR_NAME = re.compile(r"<name>(.*?)</name>", re.S)


def canon(value: str) -> str:
    text = str(value).replace("&", " and ").replace("/", " ")
    text = re.sub(r"[^A-Za-z0-9\s]", " ", text)
    tokens = [t for t in re.split(r"\s+", text.upper()) if t]
    return " ".join(t for t in tokens if not SUFFIX.fullmatch(t))


def compact(value: str) -> str:
    return canon(value).replace(" ", "")


def fetch(name: str, user_agent: str) -> bytes:
    query = urllib.parse.urlencode({
        "action": "getcompany",
        "company": name,
        "type": "10-K",
        "dateb": "",
        "owner": "include",
        "count": 100,
        "output": "atom",
    })
    request = urllib.request.Request(
        f"https://www.sec.gov/cgi-bin/browse-edgar?{query}",
        headers={"User-Agent": user_agent, "Accept-Encoding": "identity"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
        if response.status != 200 or not payload:
            raise RuntimeError(f"HTTP {response.status}")
        return payload


COMPANY_INFO_NAME = re.compile(r'<company-info name="([^"]+)"')

def candidates(payload_text: str) -> list[tuple[str, str]]:
    """Return (conformed_name, cik10) pairs found in an Atom feed.

    Single-match feeds carry one <author><name>; multi-match feeds carry
    one <entry> block per company with a CIK reference and a
    <company-info name="..."> attribute.
    """
    pairs: list[tuple[str, str]] = []
    for chunk in payload_text.split("<entry")[1:]:
        cik_match = ENTRY_CIK.search(chunk)
        if not cik_match:
            continue
        info_match = COMPANY_INFO_NAME.search(chunk)
        name_match = AUTHOR_NAME.search(chunk)
        conformed = ""
        if info_match:
            conformed = info_match.group(1)
        elif name_match:
            conformed = str(name_match.group(1))
        conformed = " ".join(conformed.split())
        if not conformed or conformed.upper() == "WEBMASTER":
            continue
        pairs.append((conformed, f"{int(cik_match.group(1)):010d}"))
    return pairs


def main() -> None:
    user_agent = _require_sec_user_agent(None)
    store = USPITStore("data/us_pit")
    bound_names = json.load(open("tmp/exited_bound_names.json", encoding="utf-8"))
    pending = [n for n in json.load(open("tmp/exited_names.json", encoding="utf-8"))
               if n not in set(bound_names)]
    observed = datetime.now(timezone.utc).isoformat()
    dependencies = []
    newly_bound: dict[str, dict] = {}
    failures = []
    for index, name in enumerate(pending):
        target_canon = canon(name)
        target_compact = compact(name)
        chosen = None
        tried = []
        variants = [name, canon(name), " ".join(canon(name).split()[:2]),
                    canon(name).split()[0] if canon(name).split() else ""]
        seen_queries = set()
        for variant in variants:
            variant = variant.strip()
            if not variant or variant in seen_queries:
                continue
            seen_queries.add(variant)
            try:
                payload = fetch(variant, user_agent)
            except Exception as exc:  # noqa: BLE001
                failures.append((f"{name} [{variant}]", str(exc)))
                time.sleep(0.3)
                continue
            text = payload.decode("utf-8", errors="replace")
            pairs = candidates(text)
            tried.append((variant, len(pairs)))
            matches = [
                (conformed, cik) for conformed, cik in pairs
                if canon(conformed) == target_canon
                or compact(conformed) == target_compact
            ]
            unique_ciks = {cik for _, cik in matches}
            if len(unique_ciks) == 1:
                chosen = {
                    "query_variant": variant,
                    "response_bytes": payload,
                    "matched_conformed_name": matches[0][0],
                    "matched_cik": matches[0][1],
                }
                break
            time.sleep(0.2)
        if chosen is None:
            print(f"STILL-UNBOUND {name}: tried {tried}")
            continue
        digest_ref = store.put_bytes(
            chosen["response_bytes"], media_type="application/atom+xml"
        )
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
                url="https://www.sec.gov/cgi-bin/browse-edgar?"
                    + urllib.parse.urlencode({"company": chosen["query_variant"],
                                              "type": "10-K", "output": "atom"}),
                dataset="sec_company_search",
                metadata={
                    "artifact_kind": "raw_edgar_company_search_atom",
                    "query_company": chosen["query_variant"],
                    "review_name": name,
                    "matched_conformed_name": chosen["matched_conformed_name"],
                    "matched_cik": chosen["matched_cik"],
                    "response_sha256": digest_ref.sha256,
                    "live_query_result": True,
                },
            )
        )
        newly_bound[name] = {
            "cik": chosen["matched_cik"],
            "conformed": chosen["matched_conformed_name"],
        }
        print(f"[{index + 1}/{len(pending)}] BOUND {name} -> "
              f"{chosen['matched_conformed_name']} ({chosen['matched_cik']})")
        time.sleep(0.2)
    batch_id = None
    if dependencies:
        batch = store.write_source_batch(dependencies)
        batch_id = batch.batch_id
    json.dump(
        {"batch_id": batch_id, "newly_bound": newly_bound, "failures": failures},
        open("tmp/exited_capture_round2.json", "w", encoding="utf-8"),
        indent=1,
    )
    print(json.dumps({"bound": len(newly_bound), "failed": len(failures),
                      "batch_id": batch_id}, indent=1))


if __name__ == "__main__":
    main()
