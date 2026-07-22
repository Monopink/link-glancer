from __future__ import annotations

CREATOR_ENRICHMENT_IMPL_VERSION = "v0.3.3.1"


def creator_enrichment_public_version() -> str:
    prefix, _, build = CREATOR_ENRICHMENT_IMPL_VERSION.rpartition(".")
    return prefix if prefix and build.isdigit() else CREATOR_ENRICHMENT_IMPL_VERSION
