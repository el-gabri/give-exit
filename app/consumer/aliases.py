"""Lay-language aliases: plain-language access points to citable legal units.

A lay complaint rarely shares words with the statute, so the retrieval
agreement gate could never support the unit it needs (ADR 0019). Each entry
gives one unit a few sentences in a consumer's own words; the corpus indexes
them as a separate alias chunk, which is never quoted (ADR 0022).

The file is generated offline by ``python -m app.consumer.generate_aliases``
from statute text alone and is reviewed like the rest of the corpus.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.consumer.embedding_artifacts import atomic_write

ALIASES_PATH = Path(__file__).resolve().parent / "data" / "aliases" / "aliases.json"
# Laws whose units may carry aliases (ADR 0022). The Civil Code is
# complementary, and its lay matches drifted off-topic before.
ALIAS_LAW_IDS = frozenset({"br-cdc", "br-lgpd", "br-cf"})
# "declined": the generator asked and the model found no concrete consumer
# situation in the unit, so the entry holds no aliases and is not asked again
# while the statute text and the prompt stay the same.
AliasStatus = Literal["generated", "reviewed", "rejected", "declined"]
INDEXED_ALIAS_STATUSES: frozenset[str] = frozenset({"generated", "reviewed"})
MIN_ALIASES = 2
MAX_ALIASES = 4
MIN_ALIAS_CHARS = 12
MAX_ALIAS_CHARS = 220
_LEGAL_ID = r"^br-(?:cdc|cf|lgpd|cc)-art-[a-z0-9]+(?:-[a-z0-9]+)*$"
# An alias is a lay sentence. One that names an article, a statute or a code
# could carry a citation into the index, so it is refused.
_LEGAL_REFERENCE = re.compile(
    r"§|\b(?:arts?|artigos?|leis?|incisos?|paragrafos?|alineas?|cdc|lgpd|codigos?)\b"
)
# The id of a unit's alias chunk. The retrieval gate sees only trace chunk ids,
# so it recognises alias chunks by this shape (ADR 0023).
_ALIAS_CHUNK_ID = re.compile(r":legal:[a-z0-9-]+:alias-\d{2}$")


def alias_chunk_id(document_id: str, unit_key: str) -> str:
    """The chunk id of a unit's alias chunk; it is never quoted (ADR 0022)."""
    return f"{document_id}:legal:{unit_key}:alias-01"


def is_alias_chunk_id(chunk_id: str) -> bool:
    """Whether a retrieved chunk id names an alias chunk rather than statute text."""
    return _ALIAS_CHUNK_ID.search(chunk_id) is not None


def normalize_alias(text: str) -> str:
    """The stored form of an alias: whitespace collapsed."""
    return " ".join(text.split())


def alias_problem(text: str) -> str | None:
    """Why ``text`` cannot be an alias, or None when it can."""
    if len(text) < MIN_ALIAS_CHARS:
        return f"is shorter than {MIN_ALIAS_CHARS} characters"
    if len(text) > MAX_ALIAS_CHARS:
        return f"is longer than {MAX_ALIAS_CHARS} characters"
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    match = _LEGAL_REFERENCE.search(folded)
    if match:
        return f"cites the law ({match.group(0)!r})"
    return None


class AliasEntry(BaseModel):
    """The aliases of one unit (or of a provision without units)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_key: str = Field(pattern=_LEGAL_ID)
    provision_id: str = Field(pattern=_LEGAL_ID)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aliases: tuple[str, ...] = ()
    status: AliasStatus = "generated"
    # The generator prompt this entry came from; None for entries written before
    # entries recorded it. A generated entry from an older prompt is regenerated.
    prompt_version: str | None = None

    @field_validator("aliases", mode="before")
    @classmethod
    def _normalized(cls, values: object) -> object:
        if isinstance(values, (list, tuple)):
            return tuple(normalize_alias(str(value)) for value in values)
        return values

    @model_validator(mode="after")
    def _valid(self) -> AliasEntry:
        # Messages name the unit, so a reviewer can find it among hundreds.
        own = self.unit_key == self.provision_id or self.unit_key.startswith(
            f"{self.provision_id}-"
        )
        if not own:
            raise ValueError(f"{self.unit_key}: not a unit of {self.provision_id}")
        if self.status == "declined":
            if self.aliases:
                raise ValueError(f"{self.unit_key}: a declined entry has no aliases")
        elif len(self.aliases) < MIN_ALIASES:
            raise ValueError(f"{self.unit_key}: at least {MIN_ALIASES} aliases are required")
        elif len(self.aliases) > MAX_ALIASES:
            raise ValueError(f"{self.unit_key}: at most {MAX_ALIASES} aliases are allowed")
        for alias in self.aliases:
            problem = alias_problem(alias)
            if problem:
                raise ValueError(f"{self.unit_key}: alias {alias!r} {problem}")
        if len({alias.casefold() for alias in self.aliases}) != len(self.aliases):
            raise ValueError(f"{self.unit_key}: aliases must be distinct")
        return self


class AliasSet(BaseModel):
    """The alias file: a manifest and one entry per aliased unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    review_status: Literal["requires_legal_review", "legally_reviewed"] = "requires_legal_review"
    prompt_version: str = Field(min_length=1)
    prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model: str | None = None
    generated_on: date | None = None
    entries: tuple[AliasEntry, ...] = ()

    @model_validator(mode="after")
    def _unique_units(self) -> AliasSet:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for entry in self.entries:
            if entry.unit_key in seen:
                duplicates.add(entry.unit_key)
            seen.add(entry.unit_key)
        if duplicates:
            raise ValueError("duplicate alias entries: " + ", ".join(sorted(duplicates)))
        return self

    @property
    def indexed(self) -> tuple[AliasEntry, ...]:
        """The entries the corpus indexes."""
        return tuple(entry for entry in self.entries if entry.status in INDEXED_ALIAS_STATUSES)

    def identity(self) -> dict[str, object]:
        """What the corpus hash covers: the manifest and every indexed entry."""
        return {
            "schema_version": self.schema_version,
            "review_status": self.review_status,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "model": self.model,
            "generated_on": self.generated_on.isoformat() if self.generated_on else None,
            "entries": [
                {
                    "unit_key": entry.unit_key,
                    "source_sha256": entry.source_sha256,
                    "aliases": list(entry.aliases),
                    "status": entry.status,
                    "prompt_version": entry.prompt_version,
                }
                for entry in self.indexed
            ],
        }


def load_alias_set(path: Path = ALIASES_PATH) -> AliasSet:
    """Read an alias file; a byte-order mark or Windows line endings are fine."""
    return AliasSet.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))


def write_alias_set(alias_set: AliasSet, path: Path = ALIASES_PATH) -> None:
    rendered = json.dumps(alias_set.model_dump(mode="json"), ensure_ascii=False, indent=2)
    atomic_write(path, (rendered + "\n").encode("utf-8"))
