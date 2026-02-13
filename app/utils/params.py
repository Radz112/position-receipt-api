from __future__ import annotations

from typing import Any


def extract_param(
    body: dict,
    name: str,
    aliases: list[str] | None = None,
    use_query_fallback: bool = False,
) -> Any:
    """
    Check locations the APIX agent might place a parameter:
      1. body.X (direct)
      2. body.body.X (nested — handled by middleware, but defensive check)
      3. body.query (only if use_query_fallback=True — for primary input params only)

    Also checks aliases (e.g., 'query' as alias for 'address').
    """
    all_names = [name] + (aliases or [])

    for key in all_names:
        # Location 1: direct
        if key in body:
            return body[key]
        # Location 2: nested body (defensive, middleware should handle)
        if isinstance(body.get("body"), dict) and key in body["body"]:
            return body["body"][key]

    # Location 3: query field (agent may combine inputs here)
    # Only for primary input parameters like address — not depth, card options, etc.
    if use_query_fallback and "query" in body:
        return body["query"]

    return None
