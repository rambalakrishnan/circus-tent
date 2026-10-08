"""SPA stabilization, DOM pruning, extraction pipeline."""

from circus_tent.parser.extraction import (
    ExtractionResult,
    batch_extract_html,
    extract_html,
    extract_page,
    schema_guided_extract,
)
from circus_tent.parser.trimmer import (
    PruneProfile,
    StabilizationResult,
    prune_html,
    prune_page,
    wait_for_stability,
)

__all__ = [
    "ExtractionResult",
    "PruneProfile",
    "StabilizationResult",
    "batch_extract_html",
    "extract_html",
    "extract_page",
    "prune_html",
    "prune_page",
    "schema_guided_extract",
    "wait_for_stability",
]
