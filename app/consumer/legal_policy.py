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

Eligibility is still not a merits decision, and the result is still marked
``requires_legal_review``.
"""

from __future__ import annotations

import re
import unicodedata

from app.consumer.schemas import LegalProvision, LegalSource
from app.schemas.trace import RetrievalTrace

LEGAL_GROUND_POLICY_VERSION = "consumer-notice-scope-eligibility-v2"
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


def provision_is_eligible(provision: LegalProvision) -> bool:
    """Whether this provision may be cited as a ground in a consumer notice.

    The consumer's issue category is deliberately not an input: it does not
    determine whether an article can lawfully appear in a notice, only how
    likely retrieval is to surface it, which the query expansion already
    handles.
    """
    if provision.source is not LegalSource.CONSUMER_DEFENSE_CODE:
        # The constitutional corpus is a small, hand-reviewed selection of
        # consumer-relevant provisions; every entry is already in scope.
        return True
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


def eligible_provisions(provisions: tuple[LegalProvision, ...]) -> tuple[LegalProvision, ...]:
    """Filter a corpus slice to what a notice may cite; used by diagnostics."""
    return tuple(provision for provision in provisions if provision_is_eligible(provision))


def _division_numeral(label: str | None, keyword: str) -> str:
    """Extract ``vi-a`` from ``CAPÍTULO VI-A DA PREVENÇÃO ...``."""
    if not label:
        return ""
    normalized = unicodedata.normalize("NFKD", label).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    match = re.search(rf"\b{keyword}\s+([ivx]+(?:-[a-z])?)\b", normalized)
    return match.group(1) if match else ""


def strongly_supported_chunk_ids(traces: list[RetrievalTrace]) -> frozenset[str]:
    """Return chunks that clear a score-type-aware retrieval safety gate.

    For reciprocal-rank fusion, a score above the best possible contribution
    from either single channel proves that both dense and lexical retrieval
    contributed. Reranker and other score scales have no portable absolute
    threshold, so they require the same chunk to rank in the top three for at
    least two independently constructed queries.

    With the category allowlist gone this is the load-bearing precision
    control: an article reaches a notice because two independent retrieval
    channels agreed on it, not because one channel matched a shared word.
    """

    supported: set[str] = set()
    corroboration: dict[str, set[int]] = {}
    for trace in traces:
        if trace.error is not None:
            continue
        if trace.score_type == "rrf_score" and trace.rrf_constant is not None:
            weights = (trace.dense_weight or 1.0, trace.lexical_weight or 1.0)
            single_channel_ceiling = max(weights) / (trace.rrf_constant + 1)
            supported.update(
                item.chunk_id
                for item in trace.results
                if item.score > single_channel_ceiling + 1e-12
            )
            continue
        for item in trace.results:
            if item.rank <= 3:
                corroboration.setdefault(item.chunk_id, set()).add(trace.query_index)

    supported.update(
        chunk_id for chunk_id, query_indexes in corroboration.items() if len(query_indexes) >= 2
    )
    return frozenset(supported)
