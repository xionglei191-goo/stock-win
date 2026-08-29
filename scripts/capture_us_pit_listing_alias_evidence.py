from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, ".")

from research_platform.us_pit import (
    LicenseClass,
    SourceDependency,
    SourceRole,
    USPITStore,
)
from research_platform.us_pit.sources_official import _require_sec_user_agent


CBOE_ACCESSION = "0001558370-18-000953"
CBOE_URL = (
    "https://www.sec.gov/Archives/edgar/data/1374310/"
    "000155837018000953/0001558370-18-000953.txt"
)
CBOE_ACCEPTED_AT = "2018-02-22T22:17:52+00:00"
CBOE_EXCERPT = (
    "The Company's common stock is listed on Cboe BZX and the NASDAQ Global "
    "Select Market under the trading symbol CBOE."
)


def _normalized_text(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Freeze official SEC evidence for reviewed listing aliases."
    )
    parser.add_argument("--store", default="data/us_pit")
    args = parser.parse_args()

    response = requests.get(
        CBOE_URL,
        headers={"User-Agent": _require_sec_user_agent(None)},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.content
    text = _normalized_text(payload)
    raw = payload.decode("utf-8", errors="replace")
    if (
        "<ACCEPTANCE-DATETIME>20180222171752" not in raw
        or re.search(r"CENTRAL INDEX KEY:\s+0001374310", raw) is None
        or CBOE_EXCERPT not in text
    ):
        raise RuntimeError("SEC complete submission does not prove the frozen CBOE alias terms")

    store = USPITStore(args.store)
    reference = store.put_bytes(
        payload, media_type="application/vnd.sec.complete-submission"
    )
    observed_at = datetime.now(timezone.utc).isoformat()
    dependency = SourceDependency(
        source_id="sec_listing_alias_evidence",
        source_version="sec-cboe-2017-10k-v1",
        role=SourceRole.SIGNAL_INPUT,
        license_class=LicenseClass.OFFICIAL_PUBLIC,
        object_sha256=reference.sha256,
        observed_at=observed_at,
        published_at=CBOE_ACCEPTED_AT,
        as_of_date="2018-01-31",
        url=CBOE_URL,
        dataset="listing_alias_evidence",
        metadata={
            "artifact_kind": "raw_complete_edgar_submission",
            "accession_number": CBOE_ACCESSION,
            "cik": "0001374310",
            "publication_time_from_payload": True,
            "accepted_at_verified_in_payload": True,
            "accepted_at": CBOE_ACCEPTED_AT,
            "listing_excerpt": CBOE_EXCERPT,
            "ticker": "CBOE",
            "exchange_mic": "BATS",
            "raw_frozen": True,
        },
    )
    batch = store.write_source_batch([dependency])
    print(
        {
            "batch_id": batch.batch_id,
            "object_sha256": reference.sha256,
            "source_id": dependency.source_id,
            "url": CBOE_URL,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
