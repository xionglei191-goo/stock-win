"""Round-3 issuer evidence capture via the official EDGAR full-text search
API (efts.sec.gov/LATEST/search-index), which returns filing hits with
registrant CIKs and display names.  One frozen response per review name.
"""
from __future__ import annotations

import json
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


def main() -> None:
    user_agent = _require_sec_user_agent(None)
    store = USPITStore("data/us_pit")
    bound = set(json.load(open("tmp/exited_bound_names.json", encoding="utf-8")))
    round2 = json.load(open("tmp/exited_capture_round2.json", encoding="utf-8"))
    bound |= set(round2.get("newly_bound", {}))
    pending = [n for n in json.load(open("tmp/exited_names.json", encoding="utf-8"))
               if n not in bound]
    observed = datetime.now(timezone.utc).isoformat()
    dependencies = []
    results: dict[str, dict] = {}
    failures = []
    for index, name in enumerate(pending):
        phrase = f'"{name}"'
        query = urllib.parse.urlencode({"q": phrase, "forms": "10-K"})
        url = f"https://efts.sec.gov/LATEST/search-index?{query}"
        request = urllib.request.Request(
            url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                if response.status != 200 or not payload:
                    raise RuntimeError(f"HTTP {response.status}")
            parsed = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            failures.append((name, str(exc)))
            print(f"FAIL {name}: {exc}")
            time.sleep(0.4)
            continue
        digest_ref = store.put_bytes(payload, media_type="application/json")
        dependencies.append(
            SourceDependency(
                source_id="sec_edgar_fulltext_search",
                source_version="edgar-fts-search-index-v1",
                role=SourceRole.SIGNAL_INPUT,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=digest_ref.sha256,
                observed_at=observed,
                published_at=observed,
                as_of_date=observed[:10],
                url=url,
                dataset="sec_company_search",
                metadata={
                    "artifact_kind": "raw_edgar_fts_response",
                    "query_company": name,
                    "response_sha256": digest_ref.sha256,
                    "live_query_result": True,
                },
            )
        )
        results[name] = {"sha256": digest_ref.sha256}
        print(f"[{index + 1}/{len(pending)}] {name}: {len(payload)} bytes")
        time.sleep(0.3)
    batch_id = None
    if dependencies:
        batch = store.write_source_batch(dependencies)
        batch_id = batch.batch_id
    json.dump(
        {"batch_id": batch_id, "results": results, "failures": failures},
        open("tmp/exited_capture_round3.json", "w", encoding="utf-8"),
        indent=1,
    )
    print(json.dumps({"captured": len(results), "failed": len(failures),
                      "batch_id": batch_id}, indent=1))


if __name__ == "__main__":
    main()
