"""Deterministic eligibility policy for cited consumer law.

Retrieval is a candidate generator, not a legal merits decision, so something
must stand between "this chunk ranked well" and "this article is an authority
in a notice". That gate used to be a per-category allowlist keyed on the
consumer's chosen issue type.

It no longer is. The issue type is a lay self-classification collected at the
top of a form: a consumer can pick the wrong one by accident, several problems
routinely share one report, and the catch-all ``other`` had no entry at all,
which made a notice impossible for exactly the people least able to categorise
their own case. Filtering authorities by that field turned a UI mistake into a
dead end.

What remains is a property of the *document*, not a guess about the problem:
an extrajudicial notice sent by one consumer to one supplier can rest on the
substantive rights in the CDC, but not on the chapters that address criminal
liability, administrative sanctions by public bodies, collective litigation or
the organisation of the consumer-protection system. That boundary comes from
the statute's own structure, so it stays stable as the corpus grows and does
not depend on anyone predicting which article a given complaint needs.

The LGPD and the Civil Code follow the same idea (ADR 0016). LGPD chapters
on public bodies, administrative sanctions, the national authority and final
provisions are excluded. The Civil Code is limited by its index scope, and
Título VI of its law of obligations (the specific contract types: sale,
services, deposit, suretyship and the like) is not cited, because on the real
retrieval stack every ground it contributed to the evaluation notices was
off-topic. Both are complementary: at most three of their grounds, only
beside a CDC ground, and none when retrieval fell back to lexical-only search.

Eligibility is still not a merits decision, and the result is still marked
``requires_legal_review``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from itertools import combinations
from typing import TypeVar

from app.consumer.schemas import LegalProvision, LegalSource
from app.consumer.statutes import division_numeral
from app.schemas.rag import DENSE_CHANNEL, LEXICAL_CHANNEL
from app.schemas.trace import RetrievalTrace

LEGAL_GROUND_POLICY_VERSION = "consumer-notice-scope-eligibility-v4"
LEGAL_GROUND_POLICY_REVIEW_STATUS = "requires_legal_review"

# CDC divisions whose subject matter cannot support an individual consumer's
# extrajudicial notice. Expressed as (title, chapter) roman numerals so the
# rule reads against the statute's structure rather than article numbers.
# ``None`` as a chapter excludes the whole title.
_EXCLUDED_CDC_DIVISIONS: frozenset[tuple[str, str | None]] = frozenset(
    {
        # Título I, Cap. VII - administrative sanctions imposed by public
        # bodies. A private notice does not apply them.
        ("i", "vii"),
        # Título II - criminal offences. Alleging a crime is not the purpose
        # of a settlement proposal and is not a consumer's to charge.
        ("ii", None),
        # Título III - defence in court: procedure, collective actions and
        # res judicata. Título III, Cap. V is the exception, because
        # over-indebtedness conciliation is a substantive consumer right.
        ("iii", "i"),
        ("iii", "ii"),
        ("iii", "iii"),
        ("iii", "iv"),
        # Título IV/V/VI - the national consumer-protection system, collective
        # bargaining between associations, and the statute's own commencement.
        ("iv", None),
        ("v", None),
        ("vi", None),
    }
)

# ADR 0016: the LGPD and the Civil Code complement the CDC in a consumer
# notice; they never ground one on their own.
COMPLEMENTARY_SOURCES = frozenset({LegalSource.DATA_PROTECTION_LAW, LegalSource.CIVIL_CODE})
MAX_COMPLEMENTARY_GROUNDS = 3
# LGPD chapters an individual notice to a supplier cannot rest on: processing
# by public bodies (IV), administrative sanctions (VIII), the national
# authority and council (IX), and final and transitional provisions (X).
_EXCLUDED_LGPD_CHAPTERS = frozenset({"iv", "viii", "ix", "x"})
# Civil Code divisions inside the index scope that a notice still does not
# cite, as (part, book, title): Parte Especial, Livro I, Título VI - the
# specific contract types. On the real retrieval stack all seven grounds it
# contributed to the evaluation notices were off-topic (ADR 0016). Título VII
# (unjust enrichment, undue payment) and Título IX (civil liability) stay.
_EXCLUDED_CIVIL_CODE_DIVISIONS = frozenset({("especial", "i", "vi")})

# The two hybrid channels whose agreement makes a chunk citable.
AGREEMENT_CHANNELS = frozenset({DENSE_CHANNEL, LEXICAL_CHANNEL})
# Both channels must rank a chunk within this depth. Appearing anywhere in the
# 32-deep candidate lists was near-vacuous: once the uncitable chapters left
# the index, the offline notice evaluation cited 41 grounds, 2 of them known
# bad. At 20 it cites 20 (3 fewer than before either change), none known bad,
# and keeps its only exact hit; 16 and below lose that hit. Calibrated on the
# 22-case offline seed; recheck it on the configured embedding stack.
AGREEMENT_MAX_RANK = 20
# Without both channels, a chunk needs this rank in two independent queries.
CORROBORATION_RANK = 3

_Candidate = TypeVar("_Candidate")


def provision_is_eligible(provision: LegalProvision) -> bool:
    """Whether this provision may be cited as a ground in a consumer notice.

    The consumer's issue category is deliberately not an input: it does not
    determine whether an article can lawfully appear in a notice, only how
    likely retrieval is to surface it, which the query expansion already
    handles.
    """
    if provision.index_scope != "indexed":
        return False
    if provision.source is LegalSource.FEDERAL_CONSTITUTION:
        # The constitutional corpus is a small, hand-reviewed selection of
        # consumer-relevant provisions; every entry is already in scope.
        return True
    if provision.source is LegalSource.CIVIL_CODE:
        # The index scope already limits the Civil Code to the general part
        # and the law of obligations. The hierarchy labels come from the
        # statute parser, so they are read with the parser's own helper.
        divisions = (
            division_numeral(provision.part, "parte"),
            division_numeral(provision.book, "livro"),
            division_numeral(provision.title, "titulo"),
        )
        return divisions not in _EXCLUDED_CIVIL_CODE_DIVISIONS
    if provision.source is LegalSource.DATA_PROTECTION_LAW:
        chapter = _division_numeral(provision.chapter, "capitulo")
        return bool(chapter) and chapter not in _EXCLUDED_LGPD_CHAPTERS
    title = _division_numeral(provision.title, "titulo")
    chapter = _division_numeral(provision.chapter, "capitulo")
    if not title:
        # An article the snapshot could not place in the hierarchy is not
        # silently promoted; abstaining is the safe direction here.
        return False
    return (title, None) not in _EXCLUDED_CDC_DIVISIONS and (
        title,
        chapter,
    ) not in _EXCLUDED_CDC_DIVISIONS


def precedence_window(
    candidates: Sequence[tuple[LegalProvision, _Candidate]],
    *,
    window: int,
    lexical_only: bool,
) -> list[tuple[LegalProvision, _Candidate]]:
    """Admit rank-ordered candidates into the ground window.

    Complementary sources take at most MAX_COMPLEMENTARY_GROUNDS slots; extra
    ones are skipped without consuming a slot, so the Civil Code cannot push
    CDC articles out of the window. Without semantic retrieval they are not
    admitted at all.
    """

    admitted: list[tuple[LegalProvision, _Candidate]] = []
    complementary = 0
    for provision, candidate in candidates:
        if provision.source in COMPLEMENTARY_SOURCES:
            if lexical_only or complementary >= MAX_COMPLEMENTARY_GROUNDS:
                continue
            complementary += 1
        admitted.append((provision, candidate))
        if len(admitted) == window:
            break
    return admitted


def enforce_cdc_anchor(
    selected: Sequence[tuple[LegalProvision, _Candidate]],
) -> list[tuple[LegalProvision, _Candidate]]:
    """Complementary grounds stand only beside at least one CDC ground."""

    if any(provision.source is LegalSource.CONSUMER_DEFENSE_CODE for provision, _ in selected):
        return list(selected)
    return [
        (provision, candidate)
        for provision, candidate in selected
        if provision.source not in COMPLEMENTARY_SOURCES
    ]


def _division_numeral(label: str | None, keyword: str) -> str:
    """Extract ``vi-a`` from ``CAPÍTULO VI-A DA PREVENÇÃO ...``."""
    if not label:
        return ""
    normalized = unicodedata.normalize("NFKD", label).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    match = re.search(rf"\b{keyword}\s+([ivx]+(?:-[a-z])?)\b", normalized)
    return match.group(1) if match else ""


def strongly_supported_chunk_ids(traces: list[RetrievalTrace]) -> frozenset[str]:
    """Return chunks that clear the retrieval-agreement safety gate.

    With the category allowlist gone this is the load-bearing precision
    control: an article reaches a notice because two independent signals
    agreed on it, not because one channel matched a shared word.

    Hybrid retrieval records the rank each chunk earned in the dense and the
    lexical channel. A chunk is supported when both channels ranked it within
    ``AGREEMENT_MAX_RANK``; the gate reads that from the trace rather than
    inferring it from a fused score, so it holds for any fusion weights and
    survives a reranker, which only reorders candidates.

    A trace without both channels (lexical-only degraded mode, dense-only
    configuration) cannot show that agreement. There a chunk must rank in the
    top three for two independent queries, where independent means neither
    query's text contains the other's. The narrative ranking queries contain
    the complaint and the remedy, so they never corroborate each other or
    those two; the complaint and the remedy searched separately can.
    """

    supported: set[str] = set()
    corroborating_queries: dict[str, set[str]] = {}
    for trace in traces:
        if trace.error is not None:
            continue
        both_channels_ran = trace.retrieval_mode == "hybrid" and trace.degraded_mode is None
        for item in trace.results:
            if both_channels_ran:
                if _channels_agree(item.channel_ranks):
                    supported.add(item.chunk_id)
            elif item.rank <= CORROBORATION_RANK:
                corroborating_queries.setdefault(item.chunk_id, set()).add(
                    _query_key(trace.query)
                )

    supported.update(
        chunk_id
        for chunk_id, queries in corroborating_queries.items()
        if _has_independent_pair(queries)
    )
    return frozenset(supported)


def _channels_agree(channel_ranks: dict[str, int]) -> bool:
    return all(
        channel_ranks.get(channel, AGREEMENT_MAX_RANK + 1) <= AGREEMENT_MAX_RANK
        for channel in AGREEMENT_CHANNELS
    )


def _query_key(query: str) -> str:
    return " ".join(query.split()).casefold()


def _has_independent_pair(queries: set[str]) -> bool:
    return any(
        left not in right and right not in left
        for left, right in combinations(sorted(queries), 2)
    )
