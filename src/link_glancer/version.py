from __future__ import annotations

from creator_enrichment.version import CREATOR_ENRICHMENT_IMPL_VERSION
from link_glancer.version_core import public_version_tag


def public_version_text() -> str:
    return public_version_tag()


def internal_version_text() -> str:
    return CREATOR_ENRICHMENT_IMPL_VERSION
