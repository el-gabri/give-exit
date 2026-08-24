"""Privacy-safe values for operational telemetry.

Logs must support correlation without becoming a second store for litigant
identifiers.  This module therefore provides two deliberately different
operations:

* ``redact_text`` replaces common Brazilian identifiers with typed markers.
* ``reference`` creates a non-reversible HMAC pseudonym for correlation.

When ``pseudonym_key`` is configured, references are stable across processes
and deployments that share that key.  Without one, a process-local random key
keeps references stable only for the current process.  The random default is
intentional: an unkeyed hash of a low-entropy CPF or CNJ number is enumerable.
The configured key is sensitive and must be stored like any other application
secret; it must never be written to logs or persisted beside the references.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from collections.abc import Iterable
from pathlib import Path

_PROCESS_PSEUDONYM_KEY = secrets.token_bytes(32)
_REFERENCE_HEX_CHARS = 16

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    flags=re.IGNORECASE,
)
_CNJ_RE = re.compile(
    r"(?<!\d)\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}(?!\d)"
)
_CNPJ_RE = re.compile(
    r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)"
)
_CPF_RE = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?55[ .-]*)?(?:\(?[1-9]\d\)?[ .-]*)?"
    r"\d{4,5}[ .-]\d{4}(?!\d)"
)
_SAFE_SUFFIX_RE = re.compile(r"\.[a-z0-9]{1,10}\Z", flags=re.IGNORECASE)
_SAFE_NAMESPACE_RE = re.compile(r"[^a-z0-9_]+")

_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_EMAIL_RE, "[EMAIL_REDACTED]"),
    (_CNJ_RE, "[CNJ_REDACTED]"),
    (_CNPJ_RE, "[CNPJ_REDACTED]"),
    (_CPF_RE, "[CPF_REDACTED]"),
    (_PHONE_RE, "[PHONE_REDACTED]"),
)


class TelemetryRedactor:
    """Create log-safe text and deterministic, non-reversible references.

    This boundary is intentionally for telemetry only.  It must not be used to
    alter domain objects, API responses, evidence, or legal reports.
    """

    def __init__(self, pseudonym_key: str | bytes | None = None) -> None:
        if pseudonym_key is None:
            self._key = _PROCESS_PSEUDONYM_KEY
        elif isinstance(pseudonym_key, str):
            self._key = pseudonym_key.encode("utf-8")
        else:
            self._key = bytes(pseudonym_key)
        if not self._key:
            raise ValueError("pseudonym_key must not be empty")

    def redact_text(
        self,
        value: object,
        *,
        sensitive_values: Iterable[str] = (),
    ) -> str:
        """Mask identifiers and explicitly supplied values in arbitrary text."""

        redacted = str(value)
        explicit_values = sorted(
            {item for item in sensitive_values if item}, key=len, reverse=True
        )
        for item in explicit_values:
            redacted = redacted.replace(item, self.reference(item, namespace="value"))
        for pattern, marker in _REDACTION_RULES:
            redacted = pattern.sub(marker, redacted)
        return redacted

    def reference(self, value: object, *, namespace: str = "ref") -> str:
        """Return a namespaced HMAC token without exposing ``value``."""

        safe_namespace = _SAFE_NAMESPACE_RE.sub(
            "_", namespace.strip().casefold()
        ).strip("_") or "ref"
        canonical = unicodedata.normalize("NFKC", str(value)).strip().casefold()
        digest = hmac.new(
            self._key,
            f"{safe_namespace}\0{canonical}".encode(),
            hashlib.sha256,
        ).hexdigest()[:_REFERENCE_HEX_CHARS]
        return f"{safe_namespace}_{digest}"

    def case_reference(self, case_number: object) -> str:
        """Correlate formatted and digits-only CNJ numbers to the same token."""

        raw = str(case_number)
        digits = "".join(character for character in raw if character.isdigit())
        canonical = digits or raw
        return self.reference(canonical, namespace="case")

    def filename_reference(self, filename: object) -> str:
        """Pseudonymize a filename while retaining only a safe file suffix."""

        raw = str(filename)
        suffix = Path(raw).suffix.casefold()
        safe_suffix = suffix if _SAFE_SUFFIX_RE.fullmatch(suffix) else ""
        return f"{self.reference(raw, namespace='file')}{safe_suffix}"


def redact_sensitive_text(value: object) -> str:
    """Convenience masking for log sites that do not need correlation."""

    return TelemetryRedactor().redact_text(value)
