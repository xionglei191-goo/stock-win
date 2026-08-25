from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .models import LicenseClass, SourceRole
from .sources import SourceAdapter, SourceArtifact, SyncRequest
from research_platform.us_tdx import TQReadOnlyClient


class TDXCurrentUSMasterAdapter(SourceAdapter):
    """Freeze the complete current TDX market=103 response as cross-check evidence.

    This is deliberately not a PIT membership source. It proves that a current
    vendor alias exists and preserves the untouched JSON-RPC response bytes for
    later audit.
    """

    source_id = "tdx_us_security_master_current"
    source_version = "tq-market-103-raw-v1"

    def __init__(self, client: TQReadOnlyClient | None = None) -> None:
        self.client = client or TQReadOnlyClient(timeout_seconds=30.0)

    def fetch(self, request: SyncRequest) -> tuple[SourceArtifact, ...]:
        envelope = self.client.call_raw(
            "get_stock_list", {"market": "103", "list_type": 1}
        )
        value = envelope.value
        if not isinstance(value, list) or not value:
            raise ValueError("TDX market=103 returned no current US securities")
        codes: list[str] = []
        for row in value:
            if not isinstance(row, dict):
                raise ValueError("TDX market=103 contains a non-object row")
            code = str(row.get("Code") or row.get("code") or "").strip().upper()
            if not code.endswith(".US"):
                raise ValueError(f"TDX market=103 contains a non-US code: {code!r}")
            codes.append(code)
        if len(codes) != len(set(codes)):
            raise ValueError("TDX market=103 contains duplicate security codes")
        captured_at = envelope.fetched_at.astimezone(timezone.utc)
        # SourceArtifact.observed_at is the caller's causal sync boundary.
        # The later HTTP completion instant is still frozen inside the raw
        # wrapper and metadata, but must not make the artifact appear to have
        # been available after the declared observation.
        observed_at = request.observed_at
        payload = json.dumps(
            {
                "method": envelope.method,
                "request_utf8": envelope.request_bytes.decode("utf-8"),
                "response_utf8": envelope.response_bytes.decode("utf-8"),
                "fetched_at": captured_at.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return (
            SourceArtifact(
                dataset="us_security_master_current",
                payload=payload,
                media_type="application/json",
                url="http://127.0.0.1:17709/get_stock_list?market=103&list_type=1",
                observed_at=observed_at,
                as_of_date=observed_at.date(),
                published_at=observed_at,
                role=SourceRole.CROSS_CHECK,
                license_class=LicenseClass.LOCAL_VENDOR,
                metadata={
                    "market": "103",
                    "list_type": 1,
                    "row_count": len(codes),
                    "all_codes_us_suffix": True,
                    "unique_codes": True,
                    "raw_http_response_frozen": True,
                    "response_fetched_at": captured_at.isoformat(),
                    "membership_authority": False,
                    "may_backdate_membership": False,
                },
            ),
        )


def tdx_current_codes(payload: bytes) -> dict[str, str]:
    """Parse codes/names from one hash-verified captured response wrapper."""

    wrapper = json.loads(payload.decode("utf-8"))
    response = json.loads(str(wrapper["response_utf8"]))
    result = response.get("result")
    value = result.get("Value") if isinstance(result, dict) else None
    if not isinstance(value, list):
        raise ValueError("captured TDX current master has no Value list")
    names: dict[str, str] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("captured TDX current master row is invalid")
        code = str(row.get("Code") or row.get("code") or "").strip().upper()
        name = str(row.get("Name") or row.get("name") or code).strip()
        if not code.endswith(".US") or code in names:
            raise ValueError("captured TDX current master code is invalid or duplicated")
        names[code] = name
    return names


def canonical_us_vendor_code(value: str) -> str:
    """Normalize common vendor punctuation without inventing an alias.

    iShares exports share-class tickers such as ``BRKB``/``BFB`` while TDX
    uses ``BRK.B.US``/``BF.B.US``. This function only generates the punctuated
    candidate; callers must still require that the exact candidate exists in a
    frozen TDX market=103 response.
    """

    text = str(value).strip().upper()
    if text.endswith(".US"):
        text = text[:-3]
    text = re.sub(r"\s+", "", text)
    if not text:
        raise ValueError("US ticker is empty")
    return f"{text}.US"


def resolve_current_tdx_alias(value: str, current_codes: dict[str, str]) -> str:
    direct = canonical_us_vendor_code(value)
    if direct in current_codes:
        return direct
    stem = direct[:-3]
    if "." not in stem and "-" not in stem and len(stem) >= 3:
        punctuated = f"{stem[:-1]}.{stem[-1]}.US"
        if punctuated in current_codes:
            return punctuated
    raise ValueError(f"current TDX alias is not uniquely evidenced: {value}")


__all__ = [
    "TDXCurrentUSMasterAdapter",
    "canonical_us_vendor_code",
    "resolve_current_tdx_alias",
    "tdx_current_codes",
]
