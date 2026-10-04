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
off-topic. Both are complementary: at most three of their grounds, and none
when retrieval fell back to lexical-only search. A Civil Code ground stands
only beside a CDC ground; the LGPD may ground a notice alone (ADR 0020).

Eligibility is still not a merits decision, and the result is still marked
``requires_legal_review``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from itertools import combinations
from typing import TypeVar

from app.consumer.aliases import is_alias_chunk_id
from app.consumer.schemas import LegalProvision, LegalSource
from app.consumer.statutes import division_numeral
from app.schemas.rag import DENSE_CHANNEL, LEXICAL_CHANNEL
from app.schemas.trace import RetrievalTrace

LEGAL_GROUND_POLICY_VERSION = "consumer-notice-scope-eligibility-v6"
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

# ADR 0016: the LGPD and the Civil Code share a small, semantic-only budget of
# ground slots so they cannot crowd the CDC out of a notice.
COMPLEMENTARY_SOURCES = frozenset({LegalSource.DATA_PROTECTION_LAW, LegalSource.CIVIL_CODE})
MAX_COMPLEMENTARY_GROUNDS = 3
# Sources whose grounds stand only beside a CDC ground. The Civil Code's
# general rules drifted off-topic without one (ADR 0016). The LGPD is not here
# (ADR 0020): a data-protection complaint may have no CDC article that clears
# the agreement gate, and requiring one left such complaints without a notice.
CDC_ANCHORED_SOURCES = frozenset({LegalSource.CIVIL_CODE})
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
# 32-deep candidate lists was near-vacuous (ADR 0019). Before the lay alias
# chunks, every exact hit on the configured stack ranked within 12 in both
# channels, so 13 kept one rank of margin. Alias chunks are short paraphrases
# that agree with themselves in both channels: at 13 the configured
# development split cited 67 grounds, 2 of them known-bad, and precision fell
# below v4. At 10 it keeps every exact hit (exact recall 0.400 at both depths)
# and drops 19 unlabelled grounds and one known-bad (ADR 0023). Precision moves
# sharply between 10 and 12 on 20 paired cases. Measure other depths with the
# notice evaluation's --agreement-max-rank sweep.
AGREEMENT_MAX_RANK = 10
# Each query may contribute at most this many alias chunks to the supported
# set, best rank first. An alias chunk's two channels score the same short
# paraphrase, so its agreement is weaker evidence than agreement on statute
# text. On the configured development split the cap kept every labelled
# ground, raised precision from 0.457 to 0.491 and removed the last known-bad
# citation (ADR 0023). Official chunks are never capped.
ALIAS_SUPPORT_CAP = 3
# Without both channels, a chunk needs this rank in two independent queries.
CORROBORATION_RANK = 3

_Candidate = TypeVar("_Candidate")

# ADR 0022: units that state the supplier's or the controller's defense, or that
# exclude the law's application, get no lay aliases. Written as complaints ("the
# company says it was my fault"), such aliases match one of the most common
# dispute patterns, and the consumer's notice would then quote the opponent's
# argument ("a culpa exclusiva do consumidor ou de terceiro"). Matched on the unit
# text and its lead-in, accents and case folded.
_DEFENSE_OR_EXCLUSION_TEXT = re.compile(
    r"so nao (?:sera|serao) responsabilizad|nao se aplica"
    r"|nao (?:e|sera|serao) considerad[oa]s? (?:defeituos|dados pessoais)"
    r"|dispensada a exigencia do consentimento|sem fornecimento de consentimento"
)
# LGPD art. 7: every lawful basis other than consent (inciso I) is the
# controller's to invoke, so it is a defense too.
_CONTROLLER_LAWFUL_BASES = re.compile(r"^br-lgpd-art-7-inciso-(?!i$)[ivx]+$")


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
    """Civil Code grounds stand only beside at least one CDC ground."""

    if any(provision.source is LegalSource.CONSUMER_DEFENSE_CODE for provision, _ in selected):
        return list(selected)
    return [
        (provision, candidate)
        for provision, candidate in selected
        if provision.source not in CDC_ANCHORED_SOURCES
    ]


def alias_exclusion_reason(unit_key: str, source_text: str) -> str | None:
    """Why a unit may not carry lay aliases, or None when it may (ADR 0022)."""

    decomposed = unicodedata.normalize("NFKD", source_text.casefold())
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    if _DEFENSE_OR_EXCLUSION_TEXT.search(folded) or _CONTROLLER_LAWFUL_BASES.match(unit_key):
        return (
            "states a supplier or controller defense, or excludes the law's application; "
            "a notice must not quote it on the consumer's behalf"
        )
    return None


def _division_numeral(label: str | None, keyword: str) -> str:
    """Extract ``vi-a`` from ``CAPÍTULO VI-A DA PREVENÇÃO ...``."""
    if not label:
        return ""
    normalized = unicodedata.normalize("NFKD", label).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    match = re.search(rf"\b{keyword}\s+([ivx]+(?:-[a-z])?)\b", normalized)
    return match.group(1) if match else ""


def strongly_supported_chunk_ids(
    traces: list[RetrievalTrace],
    *,
    max_rank: int = AGREEMENT_MAX_RANK,
    alias_cap: int = ALIAS_SUPPORT_CAP,
) -> frozenset[str]:
    """Return chunks that clear the retrieval-agreement safety gate.

    With the category allowlist gone this is the load-bearing precision
    control: an article reaches a notice because two independent signals
    agreed on it, not because one channel matched a shared word.

    Hybrid retrieval records the rank each chunk earned in the dense and the
    lexical channel. A chunk is supported when both channels ranked it within
    ``AGREEMENT_MAX_RANK``; the gate reads that from the trace rather than
    inferring it from a fused score, so it holds for any fusion weights and
    survives a reranker, which only reorders candidates. ``max_rank`` exists
    so the notice evaluation can measure other depths; production uses the
    default.

    An alias chunk's two channels score the same lay paraphrase, so each
    query contributes at most ``alias_cap`` of them, best rank first; an alias
    whose channels do not agree takes no slot, and official chunks are never
    capped (ADR 0023).

    A trace without both channels (lexical-only degraded mode, dense-only
    configuration) cannot show that agreement. There a chunk must rank in the
    top three for two independent queries, where independent means neither
    query's text contains the other's. The narrative ranking queries contain
    the complaint and the remedy, so they never corroborate each other or
    those two; the complaint and the remedy searched separately can.
    """

    if alias_cap < 0:
        raise ValueError("alias_cap must not be negative")
    supported: set[str] = set()
    corroborating_queries: dict[str, set[str]] = {}
    for trace in traces:
        if trace.error is not None:
            continue
        if trace.retrieval_mode == "hybrid" and trace.degraded_mode is None:
            supported.update(_agreeing_chunk_ids(trace, max_rank, alias_cap))
            continue
        for item in trace.results:
            if item.rank <= CORROBORATION_RANK:
                corroborating_queries.setdefault(item.chunk_id, set()).add(
                    _query_key(trace.query)
                )

    supported.update(
        chunk_id
        for chunk_id, queries in corroborating_queries.items()
        if _has_independent_pair(queries)
    )
    return frozenset(supported)


def _agreeing_chunk_ids(trace: RetrievalTrace, max_rank: int, alias_cap: int) -> list[str]:
    """Chunks both channels ranked within ``max_rank``, at most ``alias_cap`` of them aliases."""

    agreeing: list[str] = []
    aliases = 0
    for item in sorted(trace.results, key=lambda result: result.rank):
        if not _channels_agree(item.channel_ranks, max_rank):
            continue
        if is_alias_chunk_id(item.chunk_id):
            if aliases == alias_cap:
                continue
            aliases += 1
        agreeing.append(item.chunk_id)
    return agreeing


def _channels_agree(channel_ranks: dict[str, int], max_rank: int) -> bool:
    return all(
        channel_ranks.get(channel, max_rank + 1) <= max_rank for channel in AGREEMENT_CHANNELS
    )


def _query_key(query: str) -> str:
    return " ".join(query.split()).casefold()


def _has_independent_pair(queries: set[str]) -> bool:
    return any(
        left not in right and right not in left
        for left, right in combinations(sorted(queries), 2)
    )
