"""The ``search`` cursor: ``base64url`` of ``{v, site, jql_sha256, page_token}``.

The rules live in docs/interface.md under "search". A cursor is untrusted input: it comes from
argv, from a file or from a consumer that may have edited it. Every failure to decode or to match
this command is exit 22 ``cursor_invalid`` (never exit 1, never an echo of the cursor).

Public API:
    encode(site, jql_sha256, page_token)   the cursor text for the next page
    decode(text) -> Cursor                 shape checks only (size, base64, depth, JSON, fields)
    check(cursor, site, jql_sha256)        the cursor belongs to this site and this compiled query
    jql_digest(jql)                        sha256 hex of the compiled JQL (UTF-8)
    valid_token(token)                     a provider ``nextPageToken`` this module can carry

Order of the checks in ``decode``, each cheap and bounded before the next: length (4096), the
base64url alphabet, canonical base64 (the text must be exactly what ``encode`` would print for the
decoded bytes, so one document has one spelling), JSON nesting depth at most 4 (an iterative,
string-aware scan on the bytes, before the parser sees them), strict UTF-8 JSON (no duplicate key,
no ``NaN``), then exactly the four keys with ``v`` the integer 1, ``site`` and ``jql_sha256``
strings and ``page_token`` a printable ASCII string of 1 to 1024 characters.

The provider's token is opaque and is only ever sent back inside a JSON string of the search body.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import TicketctlError

MAX_CURSOR = 4096
MAX_TOKEN = 1024
MAX_DEPTH = 4
VERSION = 1
_ALPHABET = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_KEYS = frozenset({"v", "site", "jql_sha256", "page_token"})
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Cursor:
    site: str
    jql_sha256: str
    page_token: str


def _invalid(why: str) -> TicketctlError:
    return TicketctlError(
        "invalid_request",
        f"the cursor is not valid ({why}); restart without --cursor",
        reason="cursor_invalid",
    )


def jql_digest(jql: str) -> str:
    return hashlib.sha256(jql.encode("utf-8")).hexdigest()


def valid_token(token: object) -> bool:
    """A printable ASCII string of 1 to 1024 characters."""
    return (
        isinstance(token, str)
        and 1 <= len(token) <= MAX_TOKEN
        and all(0x20 <= ord(c) <= 0x7E for c in token)
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def encode(site: str, jql_sha256: str, page_token: str) -> str:
    """The cursor for ``page_token``; a token this tool cannot carry is ``provider_changed``."""
    if not valid_token(page_token):
        raise TicketctlError("provider_changed", "the provider sent a page token ticketctl rejects")
    document = {"v": VERSION, "site": site, "jql_sha256": jql_sha256, "page_token": page_token}
    text = _b64(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii"))
    if len(text) > MAX_CURSOR:  # unreachable with a 1024-character token and a grammar-bound site
        raise TicketctlError("provider_changed", "the next-page cursor would be too long")
    return text


def _depth_ok(raw: bytes) -> bool:
    """Whether the JSON text nests at most ``MAX_DEPTH`` deep (strings and escapes skipped)."""
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > MAX_DEPTH:
                return False
        elif byte in (0x7D, 0x5D):
            depth -= 1
    return True


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _reject_constant(_name: str) -> Any:
    raise ValueError("not a JSON constant")


def decode(text: str) -> Cursor:
    if not isinstance(text, str) or not text or len(text.encode("utf-8", "replace")) > MAX_CURSOR:
        raise _invalid("empty or longer than 4096 bytes")
    if not _ALPHABET.fullmatch(text):
        raise _invalid("not base64url")
    bare = text.rstrip("=")
    padded = bare + "=" * (-len(bare) % 4)
    try:
        raw = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("not base64url") from None
    if _b64(raw) != bare or (len(text) != len(bare) and text != padded):
        raise _invalid("not canonical base64url")
    if not _depth_ok(raw):
        raise _invalid("nested too deep")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError):  # includes UnicodeDecodeError and the int digit limit
        raise _invalid("not JSON") from None
    if not isinstance(document, dict) or set(document) != _KEYS:
        raise _invalid("wrong fields")
    version = document["v"]
    if type(version) is not int or version != VERSION:
        raise _invalid("unknown version")
    site = document["site"]
    digest = document["jql_sha256"]
    token = document["page_token"]
    if not isinstance(site, str) or not isinstance(digest, str) or not valid_token(token):
        raise _invalid("wrong field types")
    return Cursor(site, digest, token)


def check(cursor: Cursor, site: str, jql_sha256: str) -> None:
    """The cursor was made for this site and exactly this compiled query."""
    if cursor.site != site:
        raise _invalid("it belongs to another site")
    if not _HEX64.fullmatch(cursor.jql_sha256) or cursor.jql_sha256 != jql_sha256:
        raise _invalid("it belongs to another query")


__all__ = [
    "MAX_CURSOR",
    "MAX_TOKEN",
    "Cursor",
    "check",
    "decode",
    "encode",
    "jql_digest",
    "valid_token",
]
