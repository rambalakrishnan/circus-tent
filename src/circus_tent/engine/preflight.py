"""Session-validity preflight (B3). See spec."""

from __future__ import annotations

from typing import Any

from circus_tent.parser.trimmer import prune_page


async def check_session(
    page: Any, url: str, marker: str, timeout_ms: int = 20000
) -> tuple[bool, str]:
    """Return (ok, reason). reason ∈ {"", "SESSION_EXPIRED", "PREFLIGHT_ERROR"}.

    Navigates the shard's preflight URL and asserts the authenticated marker in
    the pruned DOM text. Empty ``url`` or ``marker`` disables preflight
    (returns ``(True, "")``).
    """
    if not url or not marker:
        return True, ""
    try:
        await page.goto(url, wait_until="load", timeout=timeout_ms)
    except Exception:
        return False, "PREFLIGHT_ERROR"
    try:
        pruned = await prune_page(page, "EXTRACT")
    except Exception:
        return False, "PREFLIGHT_ERROR"
    if marker in pruned:
        return True, ""
    return False, "SESSION_EXPIRED"
