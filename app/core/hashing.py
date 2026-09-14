"""Text hashing and canonical JSON shared by every audit identifier.

Retrieval traces, legal-corpus releases, evaluation datasets and embedding
artifacts all hash text or JSON, and those identifiers must agree byte for
byte, so the encoding rules live in one place.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_hex(text: str) -> str:
    """Hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, no NaN."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    """Hex SHA-256 of ``canonical_json_bytes(value)``."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
