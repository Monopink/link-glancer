from __future__ import annotations

from link_glancer.version_core import PUBLIC_VERSION

CREATOR_ENRICHMENT_BUILD = 3
CREATOR_ENRICHMENT_IMPL_VERSION = f"v{PUBLIC_VERSION}.{CREATOR_ENRICHMENT_BUILD}"


def creator_enrichment_public_version() -> str:
    return f"v{PUBLIC_VERSION}"
