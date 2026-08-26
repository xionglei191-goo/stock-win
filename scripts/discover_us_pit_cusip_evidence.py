"""Per-case CUSIP-based issuer CIK discovery for the remaining identities.

Each remaining security carries an official CUSIP in the reviewed identity
rows.  SEC filings (prospectuses, debt indentures, N-PORTs) cite CUSIPs, so
the EDGAR full-text search restricted to filings that contain the exact
CUSIP identifies the issuing registrant's CIK independently of any name
matching.  All responses are frozen verbatim; binding decisions stay with
the standard verification flow.
"""
from __future__ import annotations

import html
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
from research_platform.us_pit.store import USPITStore  # noqa: E402
from research_platform.us_pit.sources_official import (  # noqa: E402
    _require_sec_user_agent,
)


def main() -> None:
    user_agent = _require_sec_user_agent(None)
    store = USPITStore("data/us_pit")
    review_dir = Path("data/us_pit/review_inputs/oxalpha_final_merged4").resolve()
    frame = pd_read = __import__("pandas").read_parquet(
        review_dir / "identity_review.parquet"
    )
    unbound = frame[frame["issuer_id"].fillna("").eq("")]
    targets = (
        unbound[["suggested_security_id", "issuer_name", "cusip", "isin"]]
        .drop_duplicates("suggested_security_id")
    )

    observed = datetime.now(timezone.utc).isoformat()
    dependencies = []
    results: dict[str, dict] = {}

    def fts(query: str, pages: int = 3):
        collected = {}
        for page in range(1, pages + 1):
            params = {"q": query, "forms": "10-K", "page": str(page)}
            url = f"https://efts.sec.gov/LATEST/search-index?{urllib.parse.urlencode(params)}"
            request = urllib.request.Request(
                url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
            )
            payload = http_get(request, url)
            text = html.unescape(payload.decode("utf-8", errors="replace"))
            data = json.loads(text)
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                source = hit.get("_source", {})
                ciks = [f"{int(c):010d}" for c in (source.get("ciks") or [])]
                names = [str(n) for n in (source.get("display_names") or [])]
                accession = source.get("file_type") or ""
                for cik in ciks:
                    entry = collected.setdefault(
                        cik, {"display_names": set(), "hits": 0}
                    )
                    entry["hits"] += 1
                    entry["display_names"].update(names)
            time.sleep(0.25)
        return collected

    def http_get(request, url):
        return http_get_impl(request)

    def http_get_impl(request):
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
            if response.status != 200 or not payload:
                raise RuntimeError(f"HTTP {response.status}")
            return payload

    for _, row in targets.iterrows():
        security_id = str(row["suggested_security_id"])
        issuer_name = str(row["issuer_name"])
        cusip9 = str(row.get("cusip") or "").strip().upper()
        cusip6 = cusip9[:6]
        if len(cusip9) != 9:
            report_entry = {"status": "NO_CUSIP"}
            results[security_id] = report_entry
            print(f"{security_id[-12:]} NO-CUSIP ({issuer_name})")
            continue
        found = {}
        for phrase in (f'"{cusip9}"', f'"{cusip6}"'):
            try:
                found.update(fts(phrase))
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR {phrase}: {exc}")
                time.sleep(1.0)
        top = sorted(found.items(), key=lambda kv: -kv[1]["hits"])[:8]
        print(f"{issuer_name} [{cusip9}]: {len(found)} distinct filers")
        for cik, info in top:
            sample = "; ".join(sorted(info["display_names"]))[:110]
            print(f"   {cik} hits={info['hits']} | {sample}")
        results[security_id] = {
            "issuer_name": issuer_name,
            "cusip": cusip9,
            "filers": {c: {"hits": i["hits"],
                           "display_names": sorted(i["display_names"])}
                       for c, i in found.items()},
        }
        # freeze the first-page response of the exact-CUSIP query
        try:
            params = {"q": f'"{cusip9}"', "forms": "10-K", "page": "1"}
            url = f"https://efts.sec.gov/LATEST/search-index?{urllib.parse.urlencode(params)}"
            request = urllib.request.Request(
                url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
            )
            payload = http_get_impl(request)
            digest_ref = store.put_bytes(payload, media_type="application/json")
            dependencies.append(
                {
                    "sha256": digest_ref.sha256,
                    "url": url,
                    "security_id": security_id,
                    "observed_at": observed,
                }
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  freeze ERR: {exc}")
        time.sleep(0.5)

    batch_id = None
    if dependencies:
        from research_platform.us_pit.models import (  # noqa: E402
            LicenseClass,
            SourceDependency,
            SourceRole,
        )
        deps = [
            SourceDependency(
                source_id="sec_edgar_fulltext_search",
                source_version="edgar-fts-search-index-v1",
                role=SourceRole.SIGNAL_INPUT,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=item["sha256"],
                observed_at=item["observed_at"],
                published_at=item["observed_at"],
                as_of_date=item["observed_at"][:10],
                url=item["url"],
                dataset="sec_company_search",
                metadata={
                    "artifact_kind": "raw_edgar_fts_response",
                    "security_id": item["security_id"],
                    "response_sha256": item["sha256"],
                    "live_query_result": True,
                },
            )
            for item in dependencies
        ]
        batch = store.write_source_batch(deps)
        batch_id = batch.batch_id

    out = Path("tmp/exited_cusip_discovery.json")
    out.write_text(
        json.dumps({"batch_id": batch_id, "results": results},
                   indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps({"batch_id": batch_id, "securities": len(results)}, indent=1))


if __name__ == "__main__":
    main()
