from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs


def _parse_query_string(body: dict) -> dict[str, str]:
    """Parse body.query as a URL query string (e.g. 'address=0x...&token=BRETT').
    Returns a flat dict of key→value (first value only). Cached on the body dict.
    """
    cache_key = "__parsed_query"
    if cache_key in body:
        return body[cache_key]

    raw = body.get("query", "")
    parsed: dict[str, str] = {}
    if isinstance(raw, str) and "=" in raw:
        for key, values in parse_qs(raw, keep_blank_values=True).items():
            if values:
                parsed[key] = values[0]
        body[cache_key] = parsed
    else:
        body[cache_key] = parsed
    return parsed


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
      3. Parsed from body.query as URL query string (address=0x...&token=BRETT)
      4. body.query raw (only if use_query_fallback=True — for primary input params only)

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

    # Location 3: parsed query string (agent may send "address=0x...&token=BRETT")
    if "query" in body:
        parsed = _parse_query_string(body)
        for key in all_names:
            if key in parsed:
                return parsed[key]

    # Location 4: raw query field (agent may put just the value in query)
    if use_query_fallback and "query" in body:
        return body["query"]

    return None
