"""Capture official SEC EDGAR company-search results for issuer-name binding.

One Atom response per company name, fetched from
https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=<name>
&output=atom with a declared SEC-compliant User-Agent.  Responses are frozen
verbatim into a single immutable source batch; nothing is interpreted here.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, ".")
from research_platform.us_pit.sources_official import _require_sec_user_agent
from research_platform.us_pit.models import (  # noqa: E402
    LicenseClass,
    SourceDependency,
    SourceRole,
)
from research_platform.us_pit.store import USPITStore  # noqa: E402


def main() -> None:
    names = json.load(open("tmp/exited_names.json", encoding="utf-8"))
    store = USPITStore("data/us_pit")
    observed = datetime.now(timezone.utc).isoformat()
    dependencies = []
    failures = []
    for index, name in enumerate(names):
        query = urllib.parse.urlencode({
            "action": "getcompany",
            "company": name,
            "type": "10-K",
            "dateb": "",
            "owner": "include",
            "count": 100,
            "output": "atom",
        })
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _require_sec_user_agent(None),
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                if response.status != 200 or not payload:
                    raise RuntimeError(f"HTTP {response.status}")
        except Exception as exc:  # noqa: BLE001 - record and continue honestly
            failures.append((name, str(exc)))
            print(f"FAIL {name}: {exc}")
            continue
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
                    "query_company": name,
                    "response_sha256": digest_ref.sha256,
                    "publication_time_from_payload": False,
                    "live_query_result": True,
                },
            )
        )
        print(f"[{index + 1}/{len(names)}] {name}: {len(payload)} bytes")
        time.sleep(0.2)
    if not dependencies:
        raise SystemExit("no captures succeeded")
    batch = store.write_source_batch(dependencies)
    json.dump(
        {
            "batch_id": batch.batch_id,
            "failures": failures,
            "captured": len(dependencies),
        },
        open("tmp/exited_capture_result.json", "w", encoding="utf-8"),
        indent=1,
    )
    print(json.dumps({"batch_id": batch.batch_id, "captured": len(dependencies),
                      "failed": len(failures)}, indent=1))


if __name__ == "__main__":
    main()
