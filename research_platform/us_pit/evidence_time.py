from __future__ import annotations

import pandas as pd

from .models import SourceDependency


def publication_time_verified(source: SourceDependency) -> bool:
    """Return whether the source payload proves its publication timestamp."""

    metadata = dict(source.metadata)
    published = pd.to_datetime(source.published_at, errors="coerce", utc=True)
    if pd.isna(published):
        return False
    if metadata.get("publication_time_from_payload") is True:
        accepted = pd.to_datetime(
            metadata.get("accepted_at"), errors="coerce", utc=True
        )
        if metadata.get("accepted_at_verified_in_payload") is True:
            return not pd.isna(accepted) and accepted == published
        return True
    return bool(
        source.source_id == "sec_nport_ivv"
        and metadata.get("series_id_verified_in_payload") is True
        and metadata.get("accepted_at") == source.published_at
    )


def source_available_at(source: SourceDependency) -> pd.Timestamp:
    """Resolve the causal availability time without erasing verified history.

    A payload-verified publication timestamp is usable even when the immutable
    object was captured later.  Otherwise both publication and observation are
    required and the later timestamp is the fail-closed availability boundary.
    """

    published = pd.to_datetime(source.published_at, errors="coerce", utc=True)
    observed = pd.to_datetime(source.observed_at, errors="coerce", utc=True)
    if publication_time_verified(source) and not pd.isna(published):
        return published
    if pd.isna(published) or pd.isna(observed):
        return pd.NaT
    return max(published, observed)
