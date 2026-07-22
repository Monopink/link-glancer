from __future__ import annotations

from creator_enrichment.version import (
    CREATOR_ENRICHMENT_IMPL_VERSION,
    creator_enrichment_public_version,
)
from link_glancer import __version__


def public_version_text() -> str:
    return f"v{__version__}"


def internal_version_text() -> str:
    expected_prefix = f"v{__version__}."
    if CREATOR_ENRICHMENT_IMPL_VERSION.startswith(expected_prefix):
        return CREATOR_ENRICHMENT_IMPL_VERSION
    return creator_enrichment_public_version()
