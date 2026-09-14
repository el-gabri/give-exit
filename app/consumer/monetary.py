"""Conservative extraction of explicit Brazilian-real monetary mentions.

Extracted values are candidates only. They become calculation inputs only
after the consumer explicitly confirms the corresponding fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.core.hashing import sha256_hex

_MONEY_RE = re.compile(
    r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MonetaryMention:
    amount: Decimal
    quote: str
    quote_sha256: str


def extract_brl_mentions(text: str, *, context_chars: int = 100) -> list[MonetaryMention]:
    """Return explicit R$ mentions with a short, hashable source excerpt."""
    mentions: list[MonetaryMention] = []
    seen: set[tuple[Decimal, str]] = set()
    for match in _MONEY_RE.finditer(text):
        amount = parse_brl(match.group(1))
        if amount is None or amount <= 0:
            continue
        start = max(0, match.start() - context_chars)
        end = min(len(text), match.end() + context_chars)
        quote = " ".join(text[start:end].split())[:300]
        quote_sha256 = sha256_hex(quote)
        key = (amount, quote_sha256)
        if key in seen:
            continue
        seen.add(key)
        mentions.append(MonetaryMention(amount=amount, quote=quote, quote_sha256=quote_sha256))
    return mentions


def parse_brl(raw: str) -> Decimal | None:
    if "," in raw:
        value = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") == 1 and len(raw.rsplit(".", maxsplit=1)[1]) <= 2:
        value = raw
    else:
        value = raw.replace(".", "")
    try:
        return Decimal(value)
    except InvalidOperation:
        return None
